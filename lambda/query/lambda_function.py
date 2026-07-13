import json 
import boto3 
from decimal import Decimal 
from datetime import datetime, timezone 

dynamodb = boto3.resource('dynamodb') 
table = dynamodb.Table('rag-chatbot') 

bedrock = boto3.client('bedrock-runtime') 

TOP_K = 5  # number of chunks to retrieve as context 

DEVELOPER_NAME = "Neven" 

DAILY_QUERY_LIMIT = 400 

def check_and_increment_daily_count(): 
    """Atomically increment today's counter. Returns False if over limit.""" 
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d') 
    try: 
        response = table.update_item( 
            Key={'chunk_id': f'counter#{today}'}, 
            UpdateExpression='ADD query_count :inc', 
            ExpressionAttributeValues={':inc': 1}, 
            ReturnValues='UPDATED_NEW' 
        ) 
        count = int(response['Attributes']['query_count']) 
        return count <= DAILY_QUERY_LIMIT 
    except Exception: 
        return True  # never break the page over a counter 

# Canonical project order 
PROJECT_ORDER = [ 
    "Serverless Contact Form", 
    "URL Shortener", 
    "AI Image Label Generator", 
    "Price Tracker" 
] 

PROJECT_LIST_TEXT = ", ".join( 
    f"{i}. {name}" for i, name in enumerate(PROJECT_ORDER, start=1) 
) 

# Questions about the corpus as a whole, rather than about any single chunk. 
OVERVIEW_TRIGGERS = [ 
    "all projects", "all four", "every project", "list the projects", 
    "list your projects", "list all", "what projects", "which projects", 
    "tell me about the projects", "tell me about your projects", 
    "overview of the projects", "summarise the projects", "summarize the projects" 
] 


def is_corpus_question(question): 
    """True if the question is about the set of projects, not one project.""" 
    q = question.lower() 
    return any(trigger in q for trigger in OVERVIEW_TRIGGERS) 


def detect_project_filter(question): 
    """Return the project name if the question names one, else None.""" 
    question_lower = question.lower() 
    for project in PROJECT_ORDER: 
        if project.lower() in question_lower: 
            return project 
    return None 


def get_embedding(text): 
    """Call Titan Embeddings to convert text into a vector.""" 
    response = bedrock.invoke_model( 
        modelId="amazon.titan-embed-text-v2:0", 
        body=json.dumps({"inputText": text}) 
    ) 
    response_body = json.loads(response['body'].read()) 
    return response_body['embedding'] 


def cosine_similarity(vec_a, vec_b): 
    """Compute cosine similarity between two vectors.""" 
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b)) 
    magnitude_a = sum(a * a for a in vec_a) ** 0.5 
    magnitude_b = sum(b * b for b in vec_b) ** 0.5 

    if magnitude_a == 0 or magnitude_b == 0: 
        return 0 

    return dot_product / (magnitude_a * magnitude_b) 


def get_all_chunks(): 
    """Scan the entire table and return all items.""" 
    response = table.scan() 
    items = response['Items'] 

    while 'LastEvaluatedKey' in response: 
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey']) 
        items.extend(response['Items']) 

    return [item for item in items if 'embedding' in item] 


def get_overview_chunks(all_chunks): 
    """One Overview chunk per project, in canonical order.""" 
    ordered = [] 
    for project in PROJECT_ORDER: 
        for chunk in all_chunks: 
            if chunk['source_project'] == project and chunk['section_name'] == 'Overview': 
                ordered.append(chunk) 
                break 
    return ordered 


def generate_answer(question, context_chunks): 
    """Call Claude Haiku with retrieved chunks as grounding context.""" 
    context_text = "\n\n".join([ 
        f"[{chunk['source_project']} - {chunk['section_name']}]\n{chunk['text_chunk']}" 
        for chunk in context_chunks 
    ]) 

    prompt = f"""You are a knowledgeable assistant answering questions about the AWS portfolio projects of a developer. Below is reference material drawn from their project documentation. 

Reference material: 
{context_text} 

Question: {question} 

Instructions: 
- Answer directly and confidently. Describe {DEVELOPER_NAME}'s projects in the third person, referring to {DEVELOPER_NAME} by name or simply describing what was built and why. 
- Do not speak as {DEVELOPER_NAME}. Never use "I" or "my" to refer to their work or their decisions. 
- Never mention the reference material, the context, the documentation, or the retrieval process. Do not say phrases like "based on the context" or "the provided information". Just answer. 
- If the reference material genuinely does not cover the question, say only: "I don't have information on that." Do not explain what you would need in order to answer. 
- The projects, in order, are: {PROJECT_LIST_TEXT}. Use this order whenever asked to list them or when asked about "the first project", "the third project", and so on. 
- Use markdown formatting where it aids readability. 

Answer:""" 

    response = bedrock.invoke_model( 
        modelId="au.anthropic.claude-haiku-4-5-20251001-v1:0", 
        body=json.dumps({ 
            "anthropic_version": "bedrock-2023-05-31", 
            "max_tokens": 500, 
            "messages": [ 
                {"role": "user", "content": prompt} 
            ] 
        }) 
    ) 

    response_body = json.loads(response['body'].read()) 
    return response_body['content'][0]['text'] 


def build_response(answer, top_chunks): 
    """Shared return shape for both the corpus path and the similarity path.""" 
    return { 
        'statusCode': 200, 
        'headers': { 
            'Content-Type': 'application/json', 
            'Access-Control-Allow-Origin': '*' 
        }, 
        'body': json.dumps({ 
            'answer': answer, 
            'sources': [ 
                { 
                    'project': chunk['source_project'], 
                    'section': chunk['section_name'] 
                } 
                for chunk in top_chunks 
            ] 
        }) 
    } 


def lambda_handler(event, context): 
    body = json.loads(event['body']) 
    question = body['question'] 

    if not question or not question.strip(): 
        return build_response("Please enter a question.", []) 

    if len(question) > 300: 
        return build_response("Please ask a shorter question.", []) 

    if not check_and_increment_daily_count(): 
        return build_response( 
            "This demo has reached its daily query limit. Please try again tomorrow.", 
            [] 
        ) 

    # Step 1: pull all chunks from DynamoDB 
    all_chunks = get_all_chunks() 

    # Step 2: corpus-level question, hand over every project Overview in order. 
    # Skips embedding and similarity entirely. 
    if is_corpus_question(question): 
        top_chunks = get_overview_chunks(all_chunks) 
        answer = generate_answer(question, top_chunks) 
        return build_response(answer, top_chunks) 

    # Step 3: embed the question 
    question_embedding = get_embedding(question) 

    # Step 4: if the question names a specific project, restrict to that project only 
    project_filter = detect_project_filter(question) 
    if project_filter: 
        all_chunks = [c for c in all_chunks if c['source_project'] == project_filter] 

    # Step 5: score each chunk by cosine similarity to the question 
    scored_chunks = [] 
    for chunk in all_chunks: 
        chunk_embedding = [float(x) for x in chunk['embedding']] 
        score = cosine_similarity(question_embedding, chunk_embedding) 
        scored_chunks.append((score, chunk)) 

    # Step 6: sort by score descending, take top K 
    scored_chunks.sort(key=lambda x: x[0], reverse=True) 
    top_chunks = [chunk for score, chunk in scored_chunks[:TOP_K]] 

    # Step 7: generate the grounded answer 
    answer = generate_answer(question, top_chunks) 

    return build_response(answer, top_chunks) 
