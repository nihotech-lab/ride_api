import os
import cv2
import numpy as np
import face_recognition
from flask import Flask, render_template_string, Response, jsonify, request
from base64 import b64decode
from IPython.display import display, Javascript
from google.colab.output import eval_js
import threading

app = Flask(__name__)
KNOWN_FACES_DIR = "known_faces"
os.makedirs(KNOWN_FACES_DIR, exist_ok=True)

known_face_encodings = []
known_face_names = []
attendance_records = set()

def load_known_faces():
 global known_face_encodings, known_face_names
 known_face_encodings = []
 known_face_names = []
 for file in os.listdir(KNOWN_FACES_DIR):
 if file.endswith(('.jpg', '.png', '.jpeg')):
 path = os.path.join(KNOWN_FACES_DIR, file)
 image = face_recognition.load_image_file(path)
 encodings = face_recognition.face_encodings(image)
 if encodings:
 known_face_encodings.append(encodings[0])
 known_face_names.append(os.path.splitext(file)[0])

load_known_faces()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="am">
<head>
 <meta charset="UTF-8">
 <meta name="viewport" content="width=device-width, initial-scale=1.0">
 <title>Student Attendance</title>
 <style>
 :root { --bg: #121212; --card: #1e1e1e; --text: #ffffff; --accent: #00e676; --border: #333333; }
 body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 20px; }
 .container { max-width: 1200px; margin: 0 auto; display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }
 .card { background-color: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }
 h2 { margin-top: 0; color: var(--accent); }
 #videoElement { width: 100%; border-radius: 8px; border: 2px solid var(--border); display: block; }
 form { display: flex; flex-direction: column; gap: 12px; margin-bottom: 20px; }
 input[type="text"], button { padding: 10px; border-radius: 6px; border: 1px solid var(--border); background-color: #2a2a2a; color: #fff; }
 button { background-color: var(--accent); color: #000; font-weight: bold; cursor: pointer; border: none; }
 canvas { display: none; }
 ul { list-style: none; padding: 0; }
 li { background: #2a2a2a; padding: 10px; margin-bottom: 8px; border-radius: 6px; border-left: 4px solid var(--accent); }
 </style>
</head>
<body>
 <h1 style="text-align: center; color: var(--accent);">Student Attendance System</h1>
 <div class="container">
 <div class="card">
 <h2>Live Camera</h2>
 <video id="videoElement" autoplay playsinline></video>
 <canvas id="canvas"></canvas>
 </div>
 <div>
 <div class="card" style="margin-bottom: 20px;">
 <h2>Register Student</h2>
 <form id="registerForm">
 <input type="text" id="studentName" placeholder="Student Name" required>
 <button type="button" onclick="captureAndRegister()">Capture & Register</button>
 </form>
 <div id="statusMessage"></div>
 </div>
 <div class="card">
 <h2>Attendance List</h2>
 <ul id="attendanceList"></ul>
 </div>
 </div>
 </div>
 <script>
 const video = document.getElementById('videoElement');
 const canvas = document.getElementById('canvas');
 
 // Start Webcam
 async function startWebcam() {
 try {
 const stream = await navigator.mediaDevices.getUserMedia({ video: true });
 video.srcObject = stream;
 } catch(err) { console.error("Error accessing webcam:", err); }
 }
 startWebcam();

 // Register Student
 async function captureAndRegister() {
 const name = document.getElementById('studentName').value;
 if(!name) { alert("Please enter a name"); return; }

 canvas.width = video.videoWidth;
 canvas.height = video.videoHeight;
 canvas.getContext('2d').drawImage(video, 0, 0);
 const imageData = canvas.toDataURL('image/jpeg');

 const response = await fetch('/register', {
 method: 'POST',
 headers: {'Content-Type': 'application/json'},
 body: JSON.stringify({ name: name, image: imageData })
 });

 const result = await response.json();
 const statusDiv = document.getElementById('statusMessage');
 statusDiv.innerText = result.message;
 statusDiv.style.color = result.status === 'success' ? '#00e676' : '#ff5252';
 if(result.status === 'success') document.getElementById('registerForm').reset();
 }

 // Process Attendance
 async function processAttendance() {
 if(video.readyState !== video.HAVE_ENOUGH_DATA) return;
 canvas.width = video.videoWidth;
 canvas.height = video.videoHeight;
 canvas.getContext('2d').drawImage(video, 0, 0);
 const imageData = canvas.toDataURL('image/jpeg');

 const response = await fetch('/mark_attendance', {
 method: 'POST',
 headers: {'Content-Type': 'application/json'},
 body: JSON.stringify({ image: imageData })
 });

 const result = await response.json();
 const list = document.getElementById('attendanceList');
 list.innerHTML = '';
 result.students.forEach(student => {
 const li = document.createElement('li');
 li.textContent = `✓ ${student}`;
 list.appendChild(li);
 });
 }
 setInterval(processAttendance, 5000);
 </script>
</body>
</html>
"""

@app.route('/')
def index():
 return render_template_string(HTML_TEMPLATE)

@app.route('/register', methods=['POST'])
def register():
 data = request.json
 name = data['name']
 image_data = b64decode(data['image'].split(',')[1])
 file_path = os.path.join(KNOWN_FACES_DIR, f"{name}.jpg")
 with open(file_path, 'wb') as f:
 f.write(image_data)
 load_known_faces()
 return jsonify({"status": "success", "message": f"{name} registered successfully!"})

@app.route('/mark_attendance', methods=['POST'])
def mark_attendance():
 data = request.json
 image_data = b64decode(data['image'].split(',')[1])
 nparr = np.frombuffer(image_data, np.uint8)
 frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

 rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
 face_locations = face_recognition.face_locations(rgb_frame)
 face_encodings = face_recognition.face_encodings(rgb_frame, face_locations)

 current_detected = []
 for face_encoding in face_encodings:
 matches = face_recognition.compare_faces(known_face_encodings, face_encoding)
 if True in matches:
 face_distances = face_recognition.face_distance(known_face_encodings, face_encoding)
 best_match_index = np.argmin(face_distances)
 if matches[best_match_index]:
 name = known_face_names[best_match_index]
 attendance_records.add(name)
 current_detected.append(name)

 return jsonify({"students": list(attendance_records)})

def run_app():
 app.run(port=5000)

if __name__ == '__main__':
 from pyngrok import ngrok
 port = 5000
 public_url = ngrok.connect(port).public_url
 print(' * ngrok tunnel \"{}\" -> \"http://127.0.0.1:{}\"'.format(public_url, port))
 
 threading.Thread(target=run_app).start()
