import time
import os
import uuid
import requests
from flask import Flask, request, render_template, jsonify, session
from pymongo import MongoClient
import pymongo
from pymongo.operations import SearchIndexModel
from pymongo.errors import ServerSelectionTimeoutError
from langchain_ollama import OllamaEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from pymongo.encryption import ClientEncryption, Algorithm
from bson.codec_options import CodecOptions
from bson.binary import Binary, STANDARD

app = Flask(__name__)

embeddings = OllamaEmbeddings(
    model="nomic-embed-text",
)
CHUNK_SIZE = 2000
CHUNKS_PER_QUERY = 5
DATABASE_NAME = 'mydatabase'

KEY_FILE = 'master_key.key'

# Check if the key file exists
if os.path.exists(KEY_FILE):
    # If the key file exists, read the key
    with open(KEY_FILE, 'rb') as key_file:
        local_master_key = key_file.read()
else:
    # If the key file does not exist, generate a new key
    local_master_key = os.urandom(96)
    # Write the new key to the key file
    with open(KEY_FILE, 'wb') as key_file:
        key_file.write(local_master_key)

kms_providers = {"local": {"key": local_master_key}}
key_vault_namespace = "encryption.__pymongoTestKeyVault"

# Initialize the key vault client
key_vault_client = MongoClient('mongodb://localhost/?directConnection=true')

# Initialize the main MongoClient without automatic encryption
client = MongoClient('mongodb://localhost/?directConnection=true')
db = client[DATABASE_NAME]

# Initialize ClientEncryption
try:
    client_encryption = ClientEncryption(
        kms_providers,
        key_vault_namespace,
        key_vault_client,
        CodecOptions(uuid_representation=STANDARD)
    )
except pymongo.errors.EncryptionError as e:
    # Handle encryption errors gracefully
    print(f"Error creating ClientEncryption: {e}")

if client is None or db is None:
    raise Exception(f"Failed to connect to MongoDB - Check your configuration. client={client} db={db}")

# Define function to summarize each chunk
def summarize(txt):
    url = 'http://localhost:11434/v1/completions'
    headers = {'Content-Type': 'application/json'}
    full_prompt = f"""
[INST]
<<SYS>>
You are a helpful AI assistant who summarizes context and generates excellent questions.
<</SYS>>

[context to summarize]
{str(txt)}
[/context to summarize]

Summarize the context. Provide the summary, and some helpful questions that can be answered from the context.

[RESPONSE FORMAT]
- RESPOND IN PLAINTEXT (EMOJIS ARE ALLOWED). IMPORTANT!
- USE MARKDOWN LIST FORMAT.
- MAX RESPONSE LENGTH IS 1000 WORDS.
[/RESPONSE FORMAT]
[/INST]
"""
    data = {'prompt': full_prompt, 'model': 'llama3.2:3b', 'max_tokens': 5000}
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json()['choices'][0]['text']
    except requests.RequestException as e:
        return f"Error: {e}"

def get_collection_names():
    """Retrieve the names of all collections in the database."""
    collections = list(db.list_collection_names())
    app.logger.debug(f"Collections: {collections}")
    return collections

def generate_response(prompt, conversation_history):
    """Generate an AI response based on the prompt and conversation history."""
    formatted_history = "\n".join(conversation_history)
    full_prompt = f"""
[INST]
<<SYS>>
You are a helpful AI assistant.
<</SYS>>

[chat history]
{formatted_history}
[/chat history]

{prompt}
[/INST]
"""
    print("PROMPT: " + full_prompt)
    url = 'http://localhost:11434/v1/completions'
    headers = {'Content-Type': 'application/json'}
    data = {'prompt': full_prompt, 'model': 'llama3.2:3b', 'max_tokens': 5000}

    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json()['choices'][0]['text']
    except requests.RequestException as e:
        return f"Error: {e}"

@app.route('/status')
def get_mongo_status():
    """Microservice health check"""
    try:
        temp_client = MongoClient('mongodb://localhost/?directConnection=true', serverSelectionTimeoutMS=5000)
        temp_client.server_info()
        return jsonify({"database_status": "🟢"})
    except pymongo.errors.ServerSelectionTimeoutError:
        return jsonify({"database_status": "🔴"}), 503

@app.route('/')
def index():
    return render_template('index.html', collections=get_collection_names())

@app.route('/ingest', methods=['POST'])
def ingest():
    text = str(request.json.get('text'))
    collection_name = str(request.json.get('collection_name'))
    source = str(request.json.get('source'))
    chunk_size = request.json.get('chunk_size', CHUNK_SIZE)

    # Retrieve the key alt name
    keymap_entry = db["keymap"].find_one({"collection": collection_name})
    if not keymap_entry:
        return jsonify({'error': 'Encryption key not found for the collection'}), 500

    key_alt_name = keymap_entry['alt_key']

    # Initialize the RecursiveCharacterTextSplitter
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=20,
        length_function=len,
        separators=["\n\n", "\n", " ", ""]
    )

    # Split the text into chunks
    chunks = text_splitter.split_text(text)
    new_docs = []
    for chunk in chunks:
        # Manually encrypt the text field
        encrypted_text = client_encryption.encrypt(
            chunk,
            Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Deterministic,
            key_alt_name=key_alt_name
        )

        new_doc = {
            "text": encrypted_text,
            "source": source,
            "embedding": embeddings.embed_documents([chunk])
        }
        new_docs.append(new_doc)

    if collection_name not in db.list_collection_names():
        return jsonify({'error': 'Collection does not exist'})

    try:
        insertResults = db[collection_name].insert_many(new_docs)
        if len(insertResults.inserted_ids) == len(new_docs):
            return jsonify({'text': text, 'num_documents': len(new_docs)})
        else:
            return jsonify({'error': 'Failed to insert documents'})
    except pymongo.errors.PyMongoError as e:
        print(f"Error inserting documents: {e}")
        return jsonify({'error': 'Failed to insert documents'}), 500

@app.route('/create_collection', methods=['POST'])
def create_collection():
    new_collection_name = request.json.get('name')
    if new_collection_name in db.list_collection_names():
        return jsonify({'error': 'Collection already exists'})

    alt_name = str(uuid.uuid4().hex)
    key_id = client_encryption.create_data_key("local", key_alt_names=[alt_name])

    # Create the collection
    db.create_collection(new_collection_name)

    # Create the keymap collection if it doesn't exist
    if "keymap" not in db.list_collection_names():
        db.create_collection("keymap")

    # Store the key alt name in your keymap collection
    db["keymap"].insert_one({
        "collection": new_collection_name,
        "alt_key": alt_name
    })

    # Create your index model, then create the search index
    search_index_model = SearchIndexModel(
        definition={
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": 768,
                    "similarity": "cosine"
                }
            ]
        },
        name="vector_index",
        type="vectorSearch",
    )
    result = db[new_collection_name].create_search_index(model=search_index_model)
    print("New search index named " + result + " is building.")
    # Wait for initial sync to complete
    print("Polling to check if the index is ready. This may take up to a minute.")
    predicate = lambda index: index.get("queryable") is True
    while True:
        indices = list(db[new_collection_name].list_search_indexes("vector_index"))
        if len(indices) and predicate(indices[0]):
            break
        time.sleep(5)
    print(result + " is ready for querying.")
    return jsonify({'status': 'success', 'collections': get_collection_names()})

@app.route('/list_collections', methods=['GET'])
def get_collections():
    collections = get_collection_names()
    return jsonify({'collections': collections})

@app.route('/delete_collection', methods=['POST'])
def delete_collection():
    collection_name = request.json.get('name')
    if collection_name not in db.list_collection_names():
        return jsonify({'error': 'Collection does not exist'})
    db.drop_collection(collection_name)
    return jsonify({'status': 'success', 'collections': get_collection_names()})

@app.route('/explore', methods=['GET'])
def explore():
    collection_name = request.args.get('collection')
    if collection_name not in db.list_collection_names():
        return jsonify({'error': 'Collection does not exist'})
    collection = db[collection_name]
    try:
        documents = list(collection.aggregate([
            {"$match": {}},
            {"$project": {"_id": 0, "embedding": 0}}
        ], allowDiskUse=True))

        # Manually decrypt the 'text' field
        for doc in documents:
            encrypted_text = doc['text']
            if isinstance(encrypted_text, Binary):
                doc['text'] = client_encryption.decrypt(encrypted_text)

        documents_summary = list(collection.aggregate([
            {
                '$group': {
                    '_id': '$source',
                    'texts': {
                        '$push': '$text'
                    }
                }
            },
            {
                '$project': {
                    'texts': {
                        '$slice': ['$texts', 5]
                    }
                }
            }
        ], allowDiskUse=True))

        # Manually decrypt texts in documents_summary
        for summary in documents_summary:
            decrypted_texts = []
            for text in summary['texts']:
                if isinstance(text, Binary):
                    decrypted_texts.append(client_encryption.decrypt(text))
                else:
                    decrypted_texts.append(text)
            summary['texts'] = decrypted_texts

        return jsonify({'documents': documents, 'summary': summarize(str(documents_summary))})
    except pymongo.errors.PyMongoError as e:
        print(f"Error exploring collection: {e}")
        return jsonify({'error': 'Failed to explore collection'}), 500

@app.route('/update_chunk', methods=['POST'])
def update_chunk():
    action = request.json.get('action')
    collection_name = request.json.get('collection')
    source = request.json.get('source')
    og_text = request.json.get('og_text')
    new_text = request.json.get('new_text')

    if collection_name not in db.list_collection_names():
        return jsonify({'error': 'Collection does not exist'})

    collection = db[collection_name]

    # Retrieve the key alt name
    keymap_entry = db["keymap"].find_one({"collection": collection_name})
    if not keymap_entry:
        return jsonify({'error': 'Encryption key not found for the collection'}), 500

    key_alt_name = keymap_entry['alt_key']

    if action == 'save':
        # Get new embeddings
        new_embedding = embeddings.embed_documents([new_text])

        # Encrypt the new text and original text
        encrypted_new_text = client_encryption.encrypt(
            new_text,
            Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Deterministic,
            key_alt_name=key_alt_name
        )
        encrypted_og_text = client_encryption.encrypt(
            og_text,
            Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Deterministic,
            key_alt_name=key_alt_name
        )

        # Update the document
        try:
            collection.update_many(
                {'source': source, 'text': encrypted_og_text},
                {'$set': {'text': encrypted_new_text, 'embedding': new_embedding}}
            )
        except pymongo.errors.PyMongoError as e:
            print(f"Error updating document: {e}")
            return jsonify({'error': 'Failed to update document'}), 500

    elif action == 'delete':
        # Encrypt the original text
        encrypted_og_text = client_encryption.encrypt(
            og_text,
            Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Deterministic,
            key_alt_name=key_alt_name
        )
        try:
            collection.delete_many({'source': source, 'text': encrypted_og_text})
        except pymongo.errors.PyMongoError as e:
            print(f"Error deleting document: {e}")
            return jsonify({'error': 'Failed to delete document'}), 500

    return jsonify({'og_text': og_text, 'new_text': new_text})

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_input = data.get('message', '')
    selected_collection = data.get('collection', '')
    chunk_count = int(str(data.get('chunk_count', CHUNKS_PER_QUERY)))
    print("CHUNK_COUNT: " + str(chunk_count))

    conversation_history = session.get('conversation_history', [])
    conversation_history.append(f"Human: {user_input}")

    if selected_collection not in db.list_collection_names():
        # No vector store
        ai_response = generate_response(f"""
    [user input]
    {user_input}
    [/user input]

    [RESPONSE FORMAT]
        - RESPOND IN PLAINTEXT (EMOJIS ARE ALLOWED). IMPORTANT!
        - RESPOND TO THE [user input].
    """, conversation_history)
        docs = []
    else:
        collection = db[selected_collection]
        # Generate embedding for the query
        query_embedding = embeddings.embed_query(user_input)
        # Query the vector store
        documents = list(collection.aggregate([
            {"$match": {}},
            {"$project": {"_id": 0, "embedding": 0}}
        ], allowDiskUse=True))
        docs = documents
        print("\nQuery Response:")
        print("---------------")
        print(str(docs))
        print("---------------\n")

        # Decrypt the text field in docs
        for doc in docs:
            encrypted_text = doc['text']
            if isinstance(encrypted_text, Binary):
                doc['text'] = client_encryption.decrypt(encrypted_text)

        context_texts = [doc['text'] for doc in docs]

        ai_response = generate_response(f"""
    [context]
    {str(context_texts)}
    [/context]

    USE THE [context] TO RESPOND TO [user_input=`{user_input}`]

    [RESPONSE FORMAT]
        - RESPOND IN PLAINTEXT (EMOJIS ARE ALLOWED). IMPORTANT!
        - USE THE CONTEXT TO RESPOND TO THE [user input].
        - THINK CRITICALLY AND STEP BY STEP. ONLY USE THE CONTEXT TO RESPOND.
        - DO NOT INCLUDE YOUR THOUGHT PROCESS IN YOUR RESPONSE. MAKE SURE YOUR RESPONSE IS COHERENT.
        - THINK CRITICALLY AND STEP BY STEP. ONLY USE THE CONTEXT TO RESPOND.
    [/RESPONSE FORMAT]
    """, conversation_history)

    conversation_history.append(f"AI: {ai_response}")
    session['conversation_history'] = conversation_history

    return jsonify({
        'response': ai_response,
        'full_history': conversation_history,
        'chunks': context_texts if docs else []
    })


@app.route('/clear_chat', methods=['POST'])
def clear_chat():
    session['conversation_history'] = []
    return jsonify({'status': 'success', 'message': 'Chat history cleared'})

@app.route('/clear_all', methods=['POST'])
def clear_all():
    session.clear()
    return jsonify({'status': 'success', 'message': 'All data cleared'})

@app.route('/show_session', methods=['GET'])
def show_session():
    return jsonify(dict(session))

if __name__ == "__main__":
    app.run(debug=True)
