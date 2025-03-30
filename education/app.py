from flask import Flask, render_template, Response, jsonify, request, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
import cv2
import numpy as np
import mediapipe as mp
import base64
import io
from PIL import Image
import random
import string
from datetime import datetime
import json
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Store user credentials (in-memory for demo)
users = {
    'teacher': {
        'password': generate_password_hash('pass456'),
        'role': 'teacher'
    },
    'student': {
        'password': generate_password_hash('pass123'),
        'role': 'student'
    }
}

# Store active meetings
active_meetings = {}

# Store student analytics
student_analytics = {}

# Store WebRTC streams
meeting_streams = {}

# MediaPipe Face Detection and Face Mesh
mp_face_detection = mp.solutions.face_detection
mp_face_mesh = mp.solutions.face_mesh
mp_drawing = mp.solutions.drawing_utils

face_detection = mp_face_detection.FaceDetection(min_detection_confidence=0.5)
face_mesh = mp_face_mesh.FaceMesh(
    max_num_faces=1,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

@app.route('/')
def index():
    if 'username' in session:
        if session['role'] == 'teacher':
            return redirect(url_for('teacher_dashboard'))
        else:
            return redirect(url_for('student_dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    
    if username in users and check_password_hash(users[username]['password'], password):
        session['username'] = username
        session['role'] = users[username]['role']
        
        # Check if there's a pending meeting to join
        pending_meeting_id = session.pop('pending_meeting_id', None)
        if pending_meeting_id and session['role'] == 'student':
            return jsonify({
                'success': True, 
                'role': users[username]['role'],
                'redirect': url_for('join_meeting', meeting_id=pending_meeting_id)
            })
            
        return jsonify({'success': True, 'role': users[username]['role']})
    
    return jsonify({'success': False, 'message': 'Invalid credentials'})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/teacher_dashboard')
def teacher_dashboard():
    if 'username' not in session or session['role'] != 'teacher':
        return redirect(url_for('index'))
    return render_template('teacher_dashboard.html', username=session['username'])

@app.route('/student_dashboard')
def student_dashboard():
    if 'username' not in session or session['role'] != 'student':
        return redirect(url_for('index'))
    meeting_id = request.args.get('meeting_id')
    return render_template('student_dashboard.html', username=session['username'], meeting_id=meeting_id)

@app.route('/join/<meeting_id>')
def join_meeting(meeting_id):
    if 'username' not in session:
        # Save the meeting ID in session for after login
        session['pending_meeting_id'] = meeting_id
        return redirect(url_for('index'))
    
    if meeting_id not in active_meetings:
        return "Meeting not found", 404
    
    # Students can join the meeting
    if session['role'] == 'student':
        return redirect(url_for('student_dashboard', meeting_id=meeting_id))
    else:
        # For teachers, show them that this is a join link
        return f"This is a student join link for meeting {meeting_id}. Please share this URL with your students."

@socketio.on('create_meeting')
def on_create_meeting():
    if 'username' not in session or session['role'] != 'teacher':
        emit('error', {'message': 'Unauthorized'})
        return
    
    meeting_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    teacher_sid = request.sid
    
    active_meetings[meeting_id] = {
        'teacher_sid': teacher_sid,
        'teacher_name': session['username'],
        'students': {},
        'started_at': datetime.now().isoformat(),
        'analytics': {}
    }
    
    join_room(meeting_id)
    
    join_url = url_for('join_meeting', meeting_id=meeting_id, _external=True)
    emit('meeting_created', {
        'meeting_id': meeting_id,
        'join_url': join_url
    })

@socketio.on('join_meeting')
def on_join_meeting(data):
    if 'username' not in session or session['role'] != 'student':
        emit('error', {'message': 'Unauthorized'})
        return
    
    meeting_id = data.get('meetingId')
    if not meeting_id or meeting_id not in active_meetings:
        emit('error', {'message': 'Meeting not found'})
        return
    
    student_sid = request.sid
    active_meetings[meeting_id]['students'][student_sid] = {
        'username': session['username'],
        'join_time': datetime.now().isoformat()
    }
    
    join_room(meeting_id)
    
    # Initialize student analytics
    if student_sid not in student_analytics:
        student_analytics[student_sid] = {
            'username': session['username'],
            'emotion_history': [],
            'current_emotion': None
        }
    
    # Notify teacher
    emit('student_joined', {
        'studentId': student_sid,
        'username': session['username']
    }, room=active_meetings[meeting_id]['teacher_sid'])
    
    # Notify student
    emit('meeting_joined', {
        'meetingId': meeting_id,
        'teacherName': active_meetings[meeting_id]['teacher_name']
    })

@socketio.on('end_meeting')
def on_end_meeting(data):
    if 'username' not in session or session['role'] != 'teacher':
        emit('error', {'message': 'Unauthorized'})
        return
    
    meeting_id = data.get('meetingId')
    if not meeting_id or meeting_id not in active_meetings:
        emit('error', {'message': 'Meeting not found'})
        return
    
    # Notify all students
    emit('meeting_ended', room=meeting_id)
    
    # Generate and save report
    report = generate_meeting_report(meeting_id)
    active_meetings[meeting_id]['report'] = report
    
    # Clean up
    del active_meetings[meeting_id]

@socketio.on('leave_meeting')
def on_leave_meeting(data):
    if 'username' not in session:
        return
    
    meeting_id = data.get('meetingId')
    if not meeting_id or meeting_id not in active_meetings:
        return
    
    student_sid = request.sid
    if student_sid in active_meetings[meeting_id]['students']:
        # Remove student from meeting
        student_data = active_meetings[meeting_id]['students'].pop(student_sid)
        leave_room(meeting_id)
        
        # Notify teacher
        emit('student_left', {
            'studentId': student_sid,
            'username': student_data['username']
        }, room=active_meetings[meeting_id]['teacher_sid'])

@app.route('/detect_emotions', methods=['POST'])
def detect_emotions():
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        frame_data = data['frame'].split(',')[1]
        frame_bytes = base64.b64decode(frame_data)
        
        # Convert to image
        image = Image.open(io.BytesIO(frame_bytes))
        frame = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        
        # Convert to RGB for MediaPipe
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Process with Face Mesh
        results = face_mesh.process(rgb_frame)
        
        emotions = {
            'Happy': 0.0,
            'Sad': 0.0,
            'Angry': 0.0,
            'Surprised': 0.0
        }
        
        if results.multi_face_landmarks:
            face_landmarks = results.multi_face_landmarks[0]
            
            # Extract facial features
            mouth_ratio = get_mouth_ratio(face_landmarks)
            eye_ratio = get_eye_ratio(face_landmarks)
            eyebrow_ratio = get_eyebrow_ratio(face_landmarks)
            
            # Map facial features to emotions
            emotions['Happy'] = map_to_happy(mouth_ratio, eye_ratio)
            emotions['Sad'] = map_to_sad(mouth_ratio, eyebrow_ratio)
            emotions['Angry'] = map_to_angry(eyebrow_ratio, eye_ratio)
            emotions['Surprised'] = map_to_surprised(mouth_ratio, eye_ratio)
            
            # Normalize emotions
            total = sum(emotions.values())
            if total > 0:
                emotions = {k: v/total for k, v in emotions.items()}
        
        return jsonify(emotions)
    
    except Exception as e:
        print(f"Error in detect_emotions: {str(e)}")
        return jsonify({'error': str(e)}), 500

def get_mouth_ratio(landmarks):
    # Get mouth height to width ratio
    upper_lip = landmarks.landmark[13]
    lower_lip = landmarks.landmark[14]
    left_mouth = landmarks.landmark[78]
    right_mouth = landmarks.landmark[308]
    
    mouth_height = abs(upper_lip.y - lower_lip.y)
    mouth_width = abs(left_mouth.x - right_mouth.x)
    
    return mouth_height / mouth_width if mouth_width > 0 else 0

def get_eye_ratio(landmarks):
    # Get average eye openness
    left_eye_top = landmarks.landmark[159]
    left_eye_bottom = landmarks.landmark[145]
    right_eye_top = landmarks.landmark[386]
    right_eye_bottom = landmarks.landmark[374]
    
    left_eye_height = abs(left_eye_top.y - left_eye_bottom.y)
    right_eye_height = abs(right_eye_top.y - right_eye_bottom.y)
    
    return (left_eye_height + right_eye_height) / 2

def get_eyebrow_ratio(landmarks):
    # Get eyebrow position relative to eyes
    left_eyebrow = landmarks.landmark[107]
    left_eye = landmarks.landmark[159]
    right_eyebrow = landmarks.landmark[336]
    right_eye = landmarks.landmark[386]
    
    left_ratio = abs(left_eyebrow.y - left_eye.y)
    right_ratio = abs(right_eyebrow.y - right_eye.y)
    
    return (left_ratio + right_ratio) / 2

def map_to_happy(mouth_ratio, eye_ratio):
    # Wide mouth, slightly closed eyes
    return min(1.0, max(0.0, mouth_ratio * 2.0 * (1.0 - eye_ratio)))

def map_to_sad(mouth_ratio, eyebrow_ratio):
    # Drooping mouth corners, lowered eyebrows
    return min(1.0, max(0.0, (1.0 - mouth_ratio) * eyebrow_ratio))

def map_to_angry(eyebrow_ratio, eye_ratio):
    # Lowered eyebrows, narrowed eyes
    return min(1.0, max(0.0, eyebrow_ratio * (1.0 - eye_ratio)))

def map_to_surprised(mouth_ratio, eye_ratio):
    # Open mouth, wide eyes
    return min(1.0, max(0.0, mouth_ratio * eye_ratio * 2.0))

@socketio.on('student_emotion_update')
def on_student_emotion_update(data):
    if 'username' not in session or session['role'] != 'student':
        return
    
    student_sid = request.sid
    meeting_id = data.get('meeting_id')
    emotions = data.get('emotions', {})
    
    if not meeting_id or meeting_id not in active_meetings:
        return
    
    if student_sid not in active_meetings[meeting_id]['students']:
        return
    
    # Calculate attention score
    attention_score = calculate_attention_score(emotions)
    
    # Update analytics
    if student_sid not in student_analytics:
        student_analytics[student_sid] = {
            'username': session['username'],
            'emotion_history': [],
            'current_emotion': None
        }
    
    # Add to emotion history
    student_analytics[student_sid]['emotion_history'].append({
        'timestamp': datetime.now().isoformat(),
        'emotions': emotions,
        'attention_score': attention_score
    })
    
    # Update current emotion
    max_emotion = max(emotions.items(), key=lambda x: x[1])[0]
    student_analytics[student_sid]['current_emotion'] = max_emotion
    
    # Emit update to teacher
    emit('student_analytics_update', {
        'studentId': student_sid,
        'username': session['username'],
        'emotions': emotions,
        'attentionScore': attention_score,
        'currentEmotion': max_emotion
    }, room=active_meetings[meeting_id]['teacher_sid'])

def calculate_attention_score(emotions):
    # Base score is 100
    score = 100
    
    # Deduct points for negative emotions
    if 'Sad' in emotions:
        score -= emotions['Sad'] * 30
    if 'Angry' in emotions:
        score -= emotions['Angry'] * 40
        
    # Add points for positive emotions
    if 'Happy' in emotions:
        score += emotions['Happy'] * 20
    if 'Surprised' in emotions:
        score += emotions['Surprised'] * 10
        
    # Ensure score stays between 0 and 100
    return max(0, min(100, score))

def generate_meeting_report(meeting_id):
    if meeting_id not in active_meetings:
        return None
    
    meeting = active_meetings[meeting_id]
    report = {
        'meeting_id': meeting_id,
        'teacher': meeting['teacher_name'],
        'start_time': meeting['started_at'],
        'end_time': datetime.now().isoformat(),
        'students': []
    }
    
    for student_sid, student_data in meeting['students'].items():
        if student_sid in student_analytics:
            analytics = student_analytics[student_sid]
            emotion_history = analytics['emotion_history']
            
            # Calculate average emotions
            avg_emotions = {
                'Happy': 0,
                'Sad': 0,
                'Angry': 0,
                'Surprised': 0
            }
            
            total_entries = len(emotion_history)
            if total_entries > 0:
                for entry in emotion_history:
                    for emotion, score in entry['emotions'].items():
                        avg_emotions[emotion] += score
                
                avg_emotions = {k: v/total_entries for k, v in avg_emotions.items()}
            
            # Calculate average attention
            avg_attention = sum(entry['attention_score'] for entry in emotion_history) / total_entries if total_entries > 0 else 0
            
            student_report = {
                'username': student_data['username'],
                'join_time': student_data['join_time'],
                'average_emotions': avg_emotions,
                'average_attention': avg_attention,
                'emotion_history': emotion_history
            }
            
            report['students'].append(student_report)
    
    return report

@socketio.on('teacher_camera_started')
def on_teacher_camera_started(data):
    meeting_id = data.get('meetingId')
    if not meeting_id or meeting_id not in active_meetings:
        emit('error', {'message': 'Invalid meeting ID'})
        return
    
    if 'streams' not in meeting_streams:
        meeting_streams[meeting_id] = {'camera': False, 'screen': False}
    
    meeting_streams[meeting_id]['camera'] = True
    emit('teacher_camera_started', room=meeting_id)

@socketio.on('teacher_camera_stopped')
def on_teacher_camera_stopped(data):
    meeting_id = data.get('meetingId')
    if not meeting_id or meeting_id not in active_meetings:
        emit('error', {'message': 'Invalid meeting ID'})
        return
    
    if meeting_id in meeting_streams:
        meeting_streams[meeting_id]['camera'] = False
        emit('teacher_camera_stopped', room=meeting_id)

@socketio.on('teacher_screen_started')
def on_teacher_screen_started(data):
    meeting_id = data.get('meetingId')
    if not meeting_id or meeting_id not in active_meetings:
        emit('error', {'message': 'Invalid meeting ID'})
        return
    
    if 'streams' not in meeting_streams:
        meeting_streams[meeting_id] = {'camera': False, 'screen': False}
    
    meeting_streams[meeting_id]['screen'] = True
    emit('teacher_screen_started', room=meeting_id)

@socketio.on('teacher_screen_stopped')
def on_teacher_screen_stopped(data):
    meeting_id = data.get('meetingId')
    if not meeting_id or meeting_id not in active_meetings:
        emit('error', {'message': 'Invalid meeting ID'})
        return
    
    if meeting_id in meeting_streams:
        meeting_streams[meeting_id]['screen'] = False
        emit('teacher_screen_stopped', room=meeting_id)

@socketio.on('disconnect')
def handle_disconnect():
    if 'username' in session:
        # Handle teacher disconnect
        if session['role'] == 'teacher':
            for meeting_id, meeting in list(active_meetings.items()):
                if meeting['teacher_sid'] == request.sid:
                    # End meeting and notify students
                    emit('meeting_ended', room=meeting_id)
                    del active_meetings[meeting_id]
        
        # Handle student disconnect
        else:
            for meeting_id, meeting in active_meetings.items():
                if request.sid in meeting['students']:
                    # Remove student from meeting
                    student_data = meeting['students'].pop(request.sid)
                    # Notify teacher
                    emit('student_left', {
                        'studentId': request.sid,
                        'username': session['username']
                    }, room=meeting['teacher_sid'])

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
