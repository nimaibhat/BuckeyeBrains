import os
from dotenv import load_dotenv
from pymongo import MongoClient
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_huggingface import HuggingFacePipeline
from langchain_community.vectorstores import FAISS
from langchain.schema import Document
from langchain.chains import RetrievalQA
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline
import torch

# In .env put
# MONGODB_URI=mongodb+srv://XXXXXXXXX.XXXXXXXX.mongodb.net/

load_dotenv()
MONGO_URI = os.getenv("MONGODB_URI")

try:
    client = MongoClient(MONGO_URI)
    collection = client["osu_faculty"]["profiles"]
    print("Successfully connected to MongoDB.")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    exit()

print("Loading embedding model...")
embedding_model = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

# Use a more reliable model - Microsoft DialoGPT or a smaller model
print("Loading language model...")
model_id = "Qwen/Qwen1.5-1.8B-Chat"
# model_id = "microsoft/DialoGPT-medium"  
# model_id = "distilgpt2"

try:
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto" if torch.cuda.is_available() else None
    )
    
    pipe = pipeline(
        "text-generation", 
        model=model, 
        tokenizer=tokenizer, 
        max_new_tokens=256,
        do_sample=True,
        temperature=0.7,
        pad_token_id=tokenizer.eos_token_id
    )
    llm = HuggingFacePipeline(pipeline=pipe)
    print("Language model loaded successfully.")
    
except Exception as e:
    print(f"Error loading language model: {e}")

# Load documents
print("Loading professor profiles...")
docs = []
profiles_with_about_me = 0
profiles_without_about_me = 0

for prof in collection.find():
    full_name = prof.get("full_name", "Unknown")
    about_me_raw = prof.get("about_me", "")
    profile_url = prof.get("profile_url", "")
    profile_path = prof.get("profile_path", "")

    about_me = ""
    if isinstance(about_me_raw, str):
        about_me = about_me_raw.strip()
    elif isinstance(about_me_raw, dict):
        about_me = about_me_raw.get("about", "")
    
    # Build content from available fields
    content_parts = [f"Professor {full_name}"]
    
    if about_me and about_me.strip():
        content_parts.append(f"About: {about_me}")
        profiles_with_about_me += 1
    else:
        content_parts.append("No detailed about me information available")
        profiles_without_about_me += 1
    
    # Join all available information
    content = ". ".join(content_parts) + "."
    
    metadata = {
        "name": full_name,
        "url": profile_url,
        "profile_path": profile_path,
        "about_me": about_me,
        "has_about_me": bool(about_me and about_me.strip())
    }
    
    docs.append(Document(page_content=content, metadata=metadata))

print(f"Loaded {len(docs)} professor profiles.")
print(f"- {profiles_with_about_me} profiles have 'about_me' sections")
print(f"- {profiles_without_about_me} profiles don't have 'about_me' sections")

if not docs:
    print("no profiles found")
    exit()

print("Creating vector store...")
try:
    vectorstore = FAISS.from_documents(docs, embedding_model)
    retriever = vectorstore.as_retriever(search_kwargs={"k": 3})  # K is the number of content docs loaded in context
    print("Vector store created successfully.")
except Exception as e:
    print(f"Error creating vector store: {e}")
    exit()

try:
    qa_chain = RetrievalQA.from_chain_type(
        llm=llm,
        retriever=retriever,
        return_source_documents=True,
        chain_type="stuff"
    )
    print("QA chain created successfully.")
except Exception as e:
    print(f"Error creating QA chain: {e}")
    exit()

print("\n" + "="*50)
print("OSU Faculty Chatbot")
print("You can ask questions like:")
print("- 'Who teaches machine learning?'")
print("- 'Tell me about professors in computer science'")
print("- 'Who has experience with data science?'")
print("="*50)

while True:
    user_question = input("\nAsk about OSU faculty (type 'exit' or 'quit' to end): ")

    if user_question.lower() in ["exit", "quit"]:
        print("Exiting chatbot. Goodbye!")
        break

    if not user_question.strip():
        print("Please enter a valid question.")
        continue

    try:
        print("Searching and generating response...")
        result = qa_chain.invoke({"query": user_question})
        
        print("\nAnswer:")
        print("-" * 40)
        print(result["result"])
        
        print("\nSources:")
        print("-" * 40)
        if result.get("source_documents"):
            for i, doc in enumerate(result["source_documents"], 1):
                name = doc.metadata.get('name', 'Unknown')
                url = doc.metadata.get('url', 'No URL')
                has_about_me = doc.metadata.get('has_about_me', False)
                
                print(f"{i}. {name}")
                if url and url != 'No URL':
                    print(f"   URL: {url}")
                if not has_about_me:
                    print(f"   Note: This profile doesn't have an 'about me' section")
                print()
        else:
            print("No source documents found for this query.")
            
    except Exception as e:
        print(f"An error occurred during inovcation: {e}")
