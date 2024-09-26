import datetime
import io
import os
import re
import json
import secrets
from typing import Counter
from cryptography.hazmat.backends import default_backend
from openai import OpenAI
import firebase_admin
import firebase_admin.auth
from google.resumable_media.requests import upload
import requests
import socket
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth
from firebase_admin import credentials, firestore
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from google.cloud import storage, vision
from google.oauth2 import service_account

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(32))
# Initialize Firebase Admin
cred = credentials.Certificate('./service.json')
firebase_admin.initialize_app(cred)
client=OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
# Initialize Firestore client
db = firestore.client()

# Initialize Google Cloud Storage client
google_credentials = service_account.Credentials.from_service_account_file('./service.json')
storage_client = storage.Client(credentials=google_credentials, project='botpressbot-6083e')

# Bucket name
bucket_name = 'imagesbucket_matt'  # Replace with your bucket name

# Firestore user collection reference
user_coll_ref = db.collection('users')
upload_counter=1
# Set the environment variable for authentication

# Define the path to the temp folder
temp_folder = os.path.expanduser("~/RestApiForBotpress/temp")

# Ensure the temp folder exists
os.makedirs(temp_folder, exist_ok=True)

def download_image_to_temp(image_url, temp_folder):
    """Download image from the given URL and save it to the temp folder."""
    response = requests.get(image_url)
    if response.status_code == 200:
        # Create a unique file name in the temp folder
        file_name = os.path.join(temp_folder, os.path.basename(image_url))
        with open(file_name, 'wb') as temp_file:
            temp_file.write(response.content)
        return file_name  # Return the path to the temp file
    else:
        raise Exception(f"Failed to download image. Status code: {response.status_code}")

def detect_labels(image_url):
    """Detects labels in the image."""
    # Initialize a Vision client
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = './service.json'
    client = vision.ImageAnnotatorClient()

    # Download the image to the temp folder
    temp_image_path = download_image_to_temp(image_url, temp_folder)

    # Load the image content from the temp file
    with io.open(temp_image_path, 'rb') as image_file:
        content = image_file.read()
        image = vision.Image(content=content)

    # Perform label detection
    response = client.label_detection(image=image)
    labels = response.label_annotations

    labels_array = [label.description for label in labels]

    if response.error.message:
        raise Exception(f'{response.error.message}')
    return labels_array

# Home route to display all endpoints
@app.route('/', methods=['GET'])
def index():
    global upload_counter
    upload_counter=1
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()

    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    return jsonify({"message": f"Welcome, {email}!"}), 200

# Check existing user endpoint
@app.route('/Check_Existing_User', methods=['POST'])
def check_user():
    data = request.get_json()
    user_id = data.get('userID')
    result = check_existing_user(user_id)
    return jsonify(result)

# Save user info endpoint
@app.route('/Save_UserData_in_Firestore', methods=['POST'])
def store_user_info():
    try:
        data = request.get_json()
        session_id = data['session']
        user_name = data['person']
        website = data['url']
        business_info = data['businessInfo']
        print(data)
        user_coll_ref.document(session_id).set({
            'userName': user_name,
            'website': website,
            'businessInfo': business_info,
            'lastUsageDate': datetime.datetime.now(),
            'freeUsageCount': 0,
            'subscriptionTier': 'Free',
            'limit': 1,
            'subscriptionStatus': 'active'
        })

        return jsonify({'Status': True, 'Message': f'Thanks, {user_name}. I’ve saved your information.'})
    except Exception:
        return jsonify({'Status': False, 'Message': 'Error'})

# Save image in bucket endpoint
@app.route('/Save_Image_in_Bucket', methods=['POST'])
def upload_image():
    global upload_counter
    convo_id = request.form.get('id')
    print("id " + convo_id)
    print(upload_counter)

    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    print("files object", request.files)
    file = request.files.get('file')

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
        
    filename = file.filename
    print(f"Uploaded file: {filename}")
    folder_path = f"{convo_id}/"

    # Create a blob object with the folder path and file name
    blob = storage_client.bucket(bucket_name).blob(f"{folder_path}{filename}")

    # Upload the file
    blob.upload_from_file(file)

    # Return success response
    file_url = f"https://storage.googleapis.com/{bucket_name}/{folder_path}{filename}"
    labels_array = detect_labels(file_url)

    # Save the labels in a subcollection under the current document
    user_doc_ref = user_coll_ref.document(convo_id)
    image_labels_ref = user_doc_ref.collection('labels')

    # Fetch the existing document for convo_id
    labels_doc = image_labels_ref.document(convo_id).get()

    if labels_doc.exists:
        # Append new labels to the existing ones
        existing_data = labels_doc.to_dict()
        existing_labels = existing_data.get("labels", [])
    else:
        existing_labels = []

    # Append the new labels to the existing labels
    existing_labels.append({
        f"image{upload_counter}": labels_array,
        "timestamp": datetime.datetime.now()
    })

    # Update the document with the new list of labels
    image_labels_ref.document(convo_id).set({
        "labels": existing_labels,
        "counter": upload_counter
    })

    upload_counter += 1
    if upload_counter>5:
        return jsonify({"error": "You have reached the limit of 5 uploads per day"}), 400

    print(jsonify({
             "message": f"File {filename} uploaded successfully!",
             "file_url": file_url,
             "last_image_label": labels_array,
             "upload_counter": upload_counter}))
    return jsonify({
        "file_name": filename,
        "file_url": file_url,
        "last_image_label": labels_array,
        "upload_counter": upload_counter  # Send upload_counter to frontend
    }), 200

# Save business info endpoint 
@app.route('/Save_businessInfo_against_UserData', methods=['POST'])
def save_business_info_endpoint():
    try:
        data = request.get_json()
        user_id = data['userID']
        business_info = data['businessInfo']
        result = save_business_info(user_id, business_info)
        return jsonify(result)
    except Exception as e:
        return jsonify({'Status': False, 'Message': f'Error: {str(e)}'})



def call_openai_api(model, messages):
    api_key = os.environ.get("OPENAI_API_KEY")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    json_data = {
        "model": model,
        "messages": messages
    }

    response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=json_data)
    return response.json()

@app.route('/Update_UserData_in_Firestore', methods=['POST'])
def update_user_info():
    try:
        data = request.get_json()
        session_id = data['session']
        new_field_value = data['newField']  # New field to be added

        # Update the document with the new field
        user_coll_ref.document(session_id).update({
            'Schedule': new_field_value,  # Adding the new field
            'lastUpdatedDate': datetime.datetime.now()  # Optionally, track when this update occurred
        })

        return jsonify({'Status': True, 'Message': 'User data updated successfully. New field added.'})
    except Exception as e:
        return jsonify({'Status': False, 'Message': f'Error: {str(e)}'})
######################## Get User Data ########################                                
@app.route('/GetPOSTDATA', methods=['GET'])
def get_post_data():
    user_coll_ref = db.collection('users')
    convo_id = request.args.get('convo_id')
    if convo_id:
        print("convo_id", convo_id)
    else:
        print("convo id not created")
    user_coll_ref = user_coll_ref.document(convo_id)
    try:
        # Get the data from the request

        user_doc = user_coll_ref.get()
        user_data = user_doc.to_dict()
        business_info = user_data.get('businessInfo')
        print("business info", business_info)
        image_labels_ref = user_coll_ref.collection('labels')

        # Fetch the existing document for convo_id
        image_labels_doc = image_labels_ref.document(convo_id).get()
        labels_data = image_labels_doc.to_dict()

        print("labels data",labels_data)
        # Extract labels from image1 to image4
        all_labels = []
        for image_key in ['image1', 'image2', 'image3', 'image4']:
            labels_list = labels_data.get(image_key, [])
            all_labels.extend(labels_list)

        # Join labels into a single string
        labels = ', '.join(all_labels)
        print('labels', labels)
        if not image_labels_doc.exists or not business_info:
            return jsonify({"error": "Both labelsData and businessInfo are required."}), 400

        business_name = business_info

        # Generate the prompt for GPT
        prompt = f"""
        Write a social media post for {business_name}. The post should highlight the latest trends related to the following topics: {labels}. Make the content engaging and include a call to action. Also, suggest a catchy headline and relevant tags. Ensure the post mentions contacting for more information.
        """
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ]

        # Call the GPT model
        response = call_openai_api(model="gpt-4o", messages=messages)
        print("openai response:", response)
        # Extract GPT output
        message_content = response['choices'][0]['message']['content']

        # Function to extract headline
        def extract_headline(content):
            # Headline usually has emojis and is in the first line
            lines = content.split('\n')
            headline = lines[0].strip()
            return headline

        # Function to extract content
        def extract_content(content):
            # Content follows the headline, stopping before the first hashtag
            content_part = content.split('\n\n#')[0].strip()
            return content_part

        # Function to extract tags
        def extract_tags(content):
            # Tags usually start after the first hashtag (#)
            tags = re.findall(r'#\w+', content)
            return ', '.join(tags)
        def get_image_urls(bucket_name, conversation_id, project_id=None):
            # Initialize the client
            storage_client = storage.Client(project=project_id)

            # Get the bucket
            bucket = storage_client.bucket(bucket_name)

            # Specify the prefix for your conversation ID
            prefix = f'{conversation_id}/'  # Assuming images are stored in folders named by conversation_id

            # List all objects with the prefix
            blobs = bucket.list_blobs(prefix=prefix)

            # Collect URLs
            image_urls = []
            for blob in blobs:
                # Generate the signed URL or public URL if the bucket is publicly accessible
                url = f"https://storage.googleapis.com/{bucket_name}/{blob.name}"
                image_urls.append(url)

            return image_urls

        # Example usage
        bucket_name = "imagesbucket_matt"
        conversation_id = convo_id
        project_id = "botpressbot-6083e"  # optional, only if you're working with a specific project
        image_urls = get_image_urls(bucket_name, conversation_id)
        image_length=len(image_urls)
        for url in image_urls:
            print(url)
        # Extract headline, content, and tags
        headline = extract_headline(message_content)
        content = extract_content(message_content)
        tags = extract_tags(message_content)

        # Save data into Firestore 'post_data' collection for the convo_id
        post_data_ref = user_coll_ref.collection("post_data").document(convo_id)
        post_data_ref.set({
            "headline": headline,
            "content": content,
            "tags": tags,
            "image_urls": image_urls,
            "image_length": image_length
        })
        return jsonify({
            "headline": headline,
            "content": content,
            "tags": tags,
            "image_urls":image_urls,
            "image_length":image_length
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

def upload_blob(bucket_name, source_file_name, destination_blob_name):


    project_id = os.environ['PROJECT_ID']

    storage_client = storage.Client(project=project_id)

    bucket = storage_client.get_bucket(bucket_name)

    blob = bucket.blob(destination_blob_name)


    blob.upload_from_filename(source_file_name)

    print(
        f"File {source_file_name} uploaded to {destination_blob_name}."
    )

def check_existing_user(user_id):
    user_doc = db.collection('users').document(user_id).get()

    if user_doc.exists:
        user_data = user_doc.to_dict()
        return {
            "found": True,
            "Data": {
                "businessInfo": user_data.get("businessInfo", ""),
                "freeUsageCount": user_data.get("freeUsageCount", 0),
                "lastImageLabels": user_data.get("lastImageLabels", ""),
                "lastUsageDate": user_data.get("lastUsageDate", ""),
                "Limit": user_data.get("limit", 0),
                "subscriptionStatus": user_data.get("subscriptionStatus", False),
                "subscriptionTier": user_data.get("subscriptionTier", "Free"),
                "userName": user_data.get("userName", ""),
                "Website": user_data.get("website", "")
            }
        }
    else:
        return {"found": False}

def save_business_info(userID, businessInfo):
    try:
        user_ref = db.collection('users').document(userID)
        user_ref.update({'businessInfo': businessInfo})
        return {"Status": True, "Message": "Business Info Updated"}
    except Exception as e:
        return {"Status": False, "Message": f"Error: {str(e)}"}
############# Get Website Address #################
@app.route('/GetWebsiteAddress', methods=['GET'])
def get_website_address():
    user_id = request.args.get('id')

    if not user_id:
        return jsonify({"error": "User ID not provided"}), 400

    # Fetch the document for the given user_id
    user_doc_ref = db.collection('users').document(user_id)
    user_doc = user_doc_ref.get()

    if user_doc.exists:
        user_data = user_doc.to_dict()
        website_address = user_data.get('website', '')
        return jsonify({"website_address": website_address})
    else:
        return jsonify({"error": "User not found"}), 404

############# Post Data Wordpress ##################        
@app.route('/post_to_wordpress', methods=['POST'])
def post_to_wordpress():
    convo_id = request.args.get('convo_id')  # Retrieve convo_id from query parameters
    data=request.get_json()
    user_name=data.get('userName')
    password=data.get('passWord')
    website_url=data.get('website')
    if not convo_id:
        return jsonify({"error": "convo_id is required"}), 400

    # Reference the 'post_data' collection under the user's document
    user_doc_ref = db.collection('users').document(convo_id)
    post_data_ref = user_doc_ref.collection("post_data")

    # Fetch the existing document for convo_id
    get_post_data = post_data_ref.document(convo_id).get()

    # Check if post data exists
    if get_post_data.exists:
        post_data = get_post_data.to_dict()
        content = post_data.get('content')
        headline = post_data.get('headline')
        image_urls = post_data.get('image_urls')

        if not headline or not content or not image_urls:
            return jsonify({"error": "Missing required data on the server"}), 400

        # Call the function to create a post on WordPress
        print("website_urls", website_url)
        status = post_creator(
            headline, 
            content, 
            image_urls, 
            # "https://getaichatbots.com",  # WordPress URL
            # "publish",  # Post status
            # "dev",  # WordPress username
            # "B8zm PPCN vGJj kTHx mpWb mNuO"   # WordPress password
            website_url,
            "publish",
            user_name,
            password    
        )

        if status == 'success':
            return jsonify({"message": "Post created successfully."}), 201
        else:
            return jsonify({"error": "Failed to create post."}), 500

    else:
        print(f"No document found for convo_id: {convo_id}")
        return jsonify({"error": f"No document found for convo_id: {convo_id}"}), 404

    #Ensure the global variables are set properly
def post_creator(title, content, image_urls, wpBaseURL, status, user_name, password):
    WP_url = f"{wpBaseURL}/wp-json/wp/v2/posts"
    auth = HTTPBasicAuth(user_name, password)

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    # Insert images into content based on the number of URLs
    image_html = ''.join([f'<p><img src="{url}" alt="Image {i+1}" /></p>\n' for i, url in enumerate(image_urls[:4])])
    content_with_images = image_html + content

    # Prepare the payload for the POST request
    payload = {
        "status": status,
        "title": title,
        "content": content_with_images
    }

    try:
        response = requests.post(
            WP_url,
            json=payload,
            headers=headers,
            auth=auth
        )

        if response.status_code == 201:
            return 'success'
        else:
            print(f"Failed to create post: {response.status_code} - {response.text}")
            return 'error'

    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return 'error'  
######## Website URL Scraping ########

def get_website_ip(url):
    try:
        # Get the IP address of the website
        ip_address = socket.gethostbyname(url.split("//")[-1].split("/")[0])  # Extracting domain from URL
        return ip_address
    except Exception as e:
        print(f"Error getting IP address: {str(e)}")
        return None

def get_location_from_ip(ip):
    try:
        # Use an external API to get location details from IP
        response = requests.get(f'http://ip-api.com/json/{ip}')
        return response.json()
    except Exception as e:
        print(f"Error getting location from IP: {str(e)}")
        return {}

def get_openai_summary(content):
    # Create a prompt to summarize the data
    prompt = f"""Generate a JSON summary from the following text {content} :
    The summary should be in the format: 
    {{"niche": "Industry/Niche", "seo_keywords": [], "pricing": {{"basic": "Basic Price","premium": "Premium Price"}}, "bio": "Short biography of key personnel", "reviews": {{"average_rating": "Rating","top_review": "Top review"}}, "additional_insights": {{"awards": ["Award 1","Award 2"], "notable_blog_post": "Title of notable blog post"}}}}."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}]
        )
        summary = response.choices[0].message.content
        clean_summary = re.sub(r'```(json)?', '', summary).strip()
        return json.loads(clean_summary)  # Return the generated JSON object
    except Exception as e:
        print(f"Error getting summary from OpenAI: {str(e)}")
        return {}

def scrapeWebsiteData(website_url):
    try:
        # Send a GET request to the website
        response = requests.get(website_url)
        response.raise_for_status()  # Check if the request was successful

        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.content, 'html.parser')
        head_content = soup.head

        # Extract location details using the website's IP
        location = {
            "country": "Unknown",  
            "city": "Unknown",
            "postal_code": "Unknown"
        }

        # Get the website's IP address
        ip_address = get_website_ip(website_url)
        if ip_address:
            geo_data = get_location_from_ip(ip_address)
            if geo_data.get('status') == 'success':
                location.update({
                    "country": geo_data.get('country', 'Unknown'),
                    "city": geo_data.get('city', 'Unknown'),
                    "postal_code": geo_data.get('zip', 'Unknown')  # Some services may use 'zip' instead of 'postal'
                })

        # Extract niche/industry

        # Get summary from OpenAI
        summary = get_openai_summary(head_content)

        # Combine all data into structured output
        output = {
            "location": location,
            "summary": summary # Replacing with OpenAI generated summary
        }

        return json.dumps(output, indent=4)

    except requests.exceptions.RequestException as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error while scraping: {str(e)}"

@app.route('/scrape', methods=['POST'])
def scrape():
    data = request.get_json()
    website_url = data.get('website_url')
    convo_id=data.get("id")
    if not website_url:
        return jsonify({"error": "No website URL provided"}), 400

    result = scrapeWebsiteData(website_url)
    post_data_ref = user_coll_ref.document(convo_id).collection("website-metadata").document()  # Create a new document in the subcollection
    post_data_ref.set({
            "details":result
        })
    return jsonify(result)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3000, debug=True)
