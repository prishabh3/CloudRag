"""
Lambda function to handle document uploads.
"""
import os
import json
import boto3
import logging
import uuid
import base64
import psycopg2
from datetime import datetime

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
secretsmanager = boto3.client('secretsmanager')
lambda_client = boto3.client('lambda')

# Get environment variables
DOCUMENTS_BUCKET = os.environ.get('DOCUMENTS_BUCKET')
METADATA_TABLE = os.environ.get('METADATA_TABLE')
DB_SECRET_ARN = os.environ.get('DB_SECRET_ARN')
STAGE = os.environ.get('STAGE')

def get_postgres_credentials():
    """
    Get PostgreSQL credentials from Secrets Manager.
    """
    try:
        secret_response = secretsmanager.get_secret_value(
            SecretId=DB_SECRET_ARN
        )
        secret = json.loads(secret_response['SecretString'])
        return secret
    except Exception as e:
        logger.error(f"Error getting PostgreSQL credentials: {str(e)}")
        raise e


def get_postgres_connection(credentials):
    """
    Get a connection to PostgreSQL.
    """
    conn = psycopg2.connect(
        host=credentials['host'],
        port=credentials['port'],
        user=credentials['username'],
        password=credentials['password'],
        dbname=credentials['dbname']
    )
    return conn


def get_mime_type(file_name):
    """
    Determine MIME type from file extension.
    
    Args:
        file_name (str): File name
        
    Returns:
        str: MIME type
    """
    file_extension = file_name.split('.')[-1].lower()
    mime_types = {
        'pdf': 'application/pdf',
        'txt': 'text/plain',
        'csv': 'text/csv',
        'doc': 'application/msword',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xls': 'application/vnd.ms-excel',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'json': 'application/json',
        'md': 'text/markdown'
    }
    return mime_types.get(file_extension, 'application/octet-stream')


def _json_response(status_code, payload):
    """Build a standard API Gateway JSON response with CORS headers."""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(payload)
    }


def list_documents(user_id):
    """
    List all documents belonging to a user, with their chunk counts.

    Args:
        user_id (str): The user whose documents should be listed

    Returns:
        dict: API Gateway response with the list of documents
    """
    try:
        credentials = get_postgres_credentials()
        conn = get_postgres_connection(credentials)
        cursor = conn.cursor()
        try:
            # One row per document_id (the newest), since a document can have both
            # an 'uploaded' row (from this handler) and a 'processed' row (from the
            # document_processor).
            cursor.execute(
                """
                SELECT DISTINCT ON (document_id)
                    document_id, file_name, mime_type, status, created_at
                FROM documents
                WHERE user_id = %s
                ORDER BY document_id, created_at DESC
                """,
                (user_id,)
            )
            doc_rows = cursor.fetchall()

            # Chunk counts for all of the user's documents in a single query.
            cursor.execute(
                "SELECT document_id, COUNT(*) FROM chunks WHERE user_id = %s GROUP BY document_id",
                (user_id,)
            )
            chunk_counts = {row[0]: row[1] for row in cursor.fetchall()}
        finally:
            cursor.close()
            conn.close()

        documents = []
        for document_id, file_name, mime_type, status, created_at in doc_rows:
            documents.append({
                'document_id': document_id,
                'file_name': file_name,
                'mime_type': mime_type,
                'status': status,
                'created_at': created_at.isoformat() if created_at else None,
                'chunk_count': chunk_counts.get(document_id, 0)
            })

        # Newest documents first.
        documents.sort(key=lambda d: d['created_at'] or '', reverse=True)

        return _json_response(200, {
            'documents': documents,
            'count': len(documents)
        })
    except Exception as e:
        logger.error(f"Error listing documents: {str(e)}")
        return _json_response(500, {'message': f"Error listing documents: {str(e)}"})


def delete_document(user_id, document_id):
    """
    Delete a document and all of its associated data (S3 objects, PostgreSQL
    rows, and DynamoDB metadata). Deletion is scoped to the requesting user so
    a user can never delete another user's document.

    Args:
        user_id (str): The owner of the document
        document_id (str): The document to delete

    Returns:
        dict: API Gateway response summarising what was deleted
    """
    if not document_id:
        return _json_response(400, {'message': 'document_id is required'})

    # 1. Delete the document's objects from S3.
    s3_objects_deleted = 0
    try:
        prefix = f"uploads/{user_id}/{document_id}/"
        listing = s3_client.list_objects_v2(Bucket=DOCUMENTS_BUCKET, Prefix=prefix)
        objects = [{'Key': obj['Key']} for obj in listing.get('Contents', [])]
        if objects:
            s3_client.delete_objects(Bucket=DOCUMENTS_BUCKET, Delete={'Objects': objects})
            s3_objects_deleted = len(objects)
    except Exception as e:
        logger.error(f"Error deleting S3 objects for {document_id}: {str(e)}")

    # 2. Delete rows from PostgreSQL (chunks first, then the document).
    chunks_deleted = 0
    documents_deleted = 0
    try:
        credentials = get_postgres_credentials()
        conn = get_postgres_connection(credentials)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM chunks WHERE document_id = %s AND user_id = %s",
                (document_id, user_id)
            )
            chunks_deleted = cursor.rowcount
            cursor.execute(
                "DELETE FROM documents WHERE document_id = %s AND user_id = %s",
                (document_id, user_id)
            )
            documents_deleted = cursor.rowcount
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        logger.error(f"Error deleting PostgreSQL rows for {document_id}: {str(e)}")

    # 3. Delete the DynamoDB metadata item (best effort).
    try:
        metadata_table = dynamodb.Table(METADATA_TABLE)
        metadata_table.delete_item(Key={'id': f"doc#{document_id}"})
    except Exception as e:
        logger.error(f"Error deleting DynamoDB item for {document_id}: {str(e)}")

    # If nothing was removed from the database, the document didn't exist for
    # this user (or was already deleted).
    if documents_deleted == 0 and chunks_deleted == 0 and s3_objects_deleted == 0:
        return _json_response(404, {
            'message': 'Document not found for this user',
            'document_id': document_id
        })

    return _json_response(200, {
        'message': 'Document deleted successfully',
        'document_id': document_id,
        'chunks_deleted': chunks_deleted,
        's3_objects_deleted': s3_objects_deleted
    })


def handler(event, context):
    """
    Lambda function to handle document uploads.
    
    Args:
        event (dict): API Gateway event containing upload details
        context (object): Lambda context
        
    Returns:
        dict: Response with status code and body
    """
    logger.info(f"Received event: {json.dumps(event)}")
    
    try:
         # Extract body from the request for API Gateway calls
        body = {}
        if 'body' in event:
            if isinstance(event.get('body'), str) and event.get('body'):
                try:
                    body = json.loads(event['body'])
                except json.JSONDecodeError:
                    body = {}
            elif isinstance(event.get('body'), dict):
                body = event.get('body')
                
        # Check if this is a health check request
        if event.get('action') == 'healthcheck' or body.get('action') == 'healthcheck':
            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'message': 'Upload handler is healthy',
                    'stage': STAGE
                })
            }

        # Document management actions (reuse the /upload route via an 'action' field)
        action = body.get('action') or event.get('action')
        if action == 'list_documents':
            return list_documents(body.get('user_id', 'system'))
        if action == 'delete_document':
            return delete_document(body.get('user_id', 'system'), body.get('document_id'))

        # Extract file data and metadata
        file_content_base64 = body.get('file_content', '')
        file_name = body.get('file_name', '')
        mime_type = body.get('mime_type', None)
        user_id = body.get('user_id', 'system')
        
        if not file_content_base64 or not file_name:
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'message': 'File content and name are required'
                })
            }
        
        # Determine MIME type if not provided
        if not mime_type:
            mime_type = get_mime_type(file_name)
            
        # Decode base64 content
        file_content = base64.b64decode(file_content_base64)
        
        # Generate a unique document ID
        document_id = str(uuid.uuid4())
        
        # Upload file to S3
        s3_key = f"uploads/{user_id}/{document_id}/{file_name}"
        s3_client.put_object(
            Bucket=DOCUMENTS_BUCKET,
            Key=s3_key,
            Body=file_content,
            ContentType=mime_type
        )
        
        # Store initial metadata in PostgreSQL
        try:
            # Get PostgreSQL credentials
            credentials = get_postgres_credentials()
            conn = get_postgres_connection(credentials)
            cursor = conn.cursor()
            
            # Insert document record
            cursor.execute("""
            INSERT INTO documents (document_id, user_id, file_name, mime_type, status, bucket, key, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                document_id,
                user_id,
                file_name,
                mime_type,
                'uploaded',
                DOCUMENTS_BUCKET,
                s3_key,
                datetime.now(),
                datetime.now()
            ))
            
            # Commit the transaction
            conn.commit()
            cursor.close()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error storing metadata in PostgreSQL: {str(e)}")
            # Continue with Storing metadata in DynamoDB as fallback
            metadata_table = dynamodb.Table(METADATA_TABLE)
            metadata_table.put_item(
                Item={
                    'id': f"doc#{document_id}",
                    'document_id': document_id,
                    'user_id': user_id,
                    'file_name': file_name,
                    'mime_type': mime_type,
                    'status': 'uploaded',
                    'bucket': DOCUMENTS_BUCKET,
                    'key': s3_key,
                    'created_at': int(datetime.now().timestamp() * 1000),
                    'updated_at': int(datetime.now().timestamp() * 1000)
                }
            )
        
        # Return success response
        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': 'File uploaded successfully',
                'document_id': document_id,
                'file_name': file_name
            })
        }
        
    except Exception as e:
        logger.error(f"Error uploading file: {str(e)}")
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': f"Error uploading file: {str(e)}"
            })
        }