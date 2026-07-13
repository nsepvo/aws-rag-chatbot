import json 
import re 
import uuid 
import boto3 
from decimal import Decimal 

dynamodb = boto3.resource('dynamodb') 
table = dynamodb.Table('rag-chatbot') 

bedrock = boto3.client('bedrock-runtime') 

SECTION_HEADERS = [ 
    "Overview", 
    "Live Demo", 
    "AWS Services Used", 
    "Request Flow", 
    "Design Decisions", 
    "Key Learnings", 
    "What I Would Do Next" 
] 

def chunk_by_sections(readme_text, project_name): 
    """Split README into chunks based on section headers.""" 
    pattern = r'(' + '|'.join(SECTION_HEADERS) + r')' 
    parts = re.split(pattern, readme_text) 

    chunks = [] 
    current_section = None 

    for part in parts: 
        part_stripped = part.strip() 
        if part_stripped in SECTION_HEADERS: 
            current_section = part_stripped 
        elif current_section and part_stripped: 
            chunks.append({ 
                "source_project": project_name, 
                "section_name": current_section, 
                "text_chunk": part_stripped 
            }) 

    return chunks 

def get_embedding(text): 
    """Call Titan Embeddings to convert text into a vector.""" 
    response = bedrock.invoke_model( 
        modelId="amazon.titan-embed-text-v2:0", 
        body=json.dumps({"inputText": text}) 
    )
    response_body = json.loads(response['body'].read()) 
    # Convert floats to Decimal for DynamoDB compatibility 
    return [Decimal(str(x)) for x in response_body['embedding']] 

def write_chunk(project_name, section_name, text_chunk): 
    embedding_input = f"{project_name} - {section_name}: {text_chunk}" 
    embedding = get_embedding(embedding_input) 
    table.put_item( 
        Item={ 
            'chunk_id': str(uuid.uuid4()), 
            'source_project': project_name, 
            'section_name': section_name, 
            'text_chunk': text_chunk, 
            'embedding': embedding 
        } 
    ) 

def lambda_handler(event, context): 
    project_name = event['project_name'] 
    written_count = 0 

    if 'readme_text' in event: 
        chunks = chunk_by_sections(event['readme_text'], project_name) 
        for chunk in chunks: 
            write_chunk(chunk['source_project'], chunk['section_name'], chunk['text_chunk']) 
            written_count += 1 

    if 'file_name' in event and 'file_content' in event: 
        write_chunk(project_name, event['file_name'], event['file_content']) 
        written_count += 1 

    return { 
        'statusCode': 200, 
        'body': json.dumps({'message': f'Ingested {written_count} chunk(s) for {project_name}'}) 
    } 
