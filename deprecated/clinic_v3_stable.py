import cv2
import mediapipe as mp
import numpy as np
import pyttsx3
import pandas as pd
import time
import queue
import threading
import os
import atexit
import json
import uuid
import logging
from logging.handlers import RotatingFileHandler
from collections import deque
from flask import Flask, render_template, Response, jsonify, request
from datetime import datetime
import werkzeug.utils

# 1. SETUP DE DIRECTORIOS IMPORTANTES Y GLOBALES ==========================
DIRS = ['uploads', 'reports', 'patients', 'logs']
for d in DIRS:
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1000 * 1024 * 1024 # 1GB Limit

# 12. ANTI-CACHE & START ===============================================
@app.after_request
def add_header(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r

# 2. LOGGING ==========================================================
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')
log_file = 'logs/podia_ai_clinic.log'
file_handler = RotatingFileHandler(log_file, mode='a', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

app_log = logging.getLogger('AsistIA_V5')
app_log.setLevel(logging.INFO)
app_log.addHandler(file_handler)
app_log.info("=== SISTEMA ASISTIA V5 INICIADO ===")

# 3. CLINIC STATE MANAGER ==============================================
class ClinicStateManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.session_data = deque(maxlen=50000)
        self.ml_features_buffer = []
        self.ml_labels_buffer = []
        self.current_live_angles = {}
        self.system_status_msg = "Esperando feed clínico..."
        
        # Cargar configuración y buffers
        self.config_db = 'config_clinic.json'
        self.buffers_db = 'ml_buffers_backup.json'
        
        self.clinical_ranges = {
            'ankle_dorsiflexion': {'min': 70, 'max': 180, 'unit': '°'},
            'knee_flexion': {'min': 150, 'max': 180, 'unit': '°'},
            'hip_extension': {'min': 150, 'max': 180, 'unit': '°'},
            'foot_progression_angle': {'min': -15, 'max': 15, 'unit': '°'} # Positivo = toe-out
        }
        
        self.current_patient_id = None
        self.load_state()
        
    def load_state(self):
        if os.path.exists(self.buffers_db):
            try:
                with open(self.buffers_db, 'r') as f:
                    data = json.load(f)
                    self.ml_features_buffer = data.get('features', [])
                    self.ml_labels_buffer = data.get('labels', [])
            except: pass
        if os.path.exists(self.config_db):
            try:
                with open(self.config_db, 'r') as f:
                    data = json.load(f)
                    self.clinical_ranges = data.get('ranges', self.clinical_ranges)
            except: pass
            
    def save_state(self):
        with self.lock:
            try:
                with open(self.buffers_db, 'w') as f:
                    json.dump({'features': self.ml_features_buffer, 'labels': self.ml_labels_buffer}, f)
                with open(self.config_db, 'w') as f:
                    json.dump({'ranges': self.clinical_ranges}, f)
            except Exception as e:
                app_log.error(f"Error guardando DB state: {e}")

state = ClinicStateManager()
atexit.register(state.save_state)

# 4. PATIENT MANAGER ====================================================
class PatientManager:
    @staticmethod
    def create_patient(data):
        pid = str(uuid.uuid4())[:8]
        patient_data = {
            'id': pid,
            'name': data.get('name', 'Anónimo'),
            'age': data.get('age', 0),
            'sex': data.get('sex', 'U'),
            'clinical_notes': data.get('clinical_notes', ''),
            'foot_type': data.get('foot_type', 'Normal'),
            'created_at': datetime.now().isoformat()
        }
        path = os.path.join('patients', f"{pid}.json")
        with open(path, 'w') as f:
            json.dump(patient_data, f)
        return pid

    @staticmethod
    def get_patient(pid):
        path = os.path.join('patients', f"{pid}.json")
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return None

# 5. VOICE ENGINE ========================================================
class VoiceEngine:
    def __init__(self):
        self.queue = queue.PriorityQueue()
        self.last_played = 0
        self.engine = None
        self.start_worker()
        
    def start_worker(self):
        def worker():
            import pythoncom
            pythoncom.CoInitialize()
            try:
                self.engine = pyttsx3.init()
                self.engine.setProperty('rate', 140)
            except Exception as e:
                app_log.error(f"TTS Falló al iniciar {e}")
                return
            while True:
                priority, _, msg = self.queue.get()
                if msg is None: break
                try:
                    self.engine.say(msg)
                    self.engine.runAndWait()
                except: pass
                self.queue.task_done()
        threading.Thread(target=worker, daemon=True).start()
        
    def alert(self, msg, priority='normal'):
        now = time.time()
        pri_num = 1 if priority == 'high' else 2
        # Debounce/Cooldown anti saturación
        if priority == 'normal' and now - self.last_played < 5.0:
            return
        self.last_played = now
        self.queue.put((pri_num, now, msg))
        app_log.info(f"TTS Ejecutado: {msg}")

voice = VoiceEngine()
atexit.register(lambda: voice.queue.put((0, time.time(), None)))

# 6. SIGNAL PROCESSOR ====================================================
class SignalProcessor:
    def __init__(self):
        self.history = {} # name -> deque
        
    def get_moving_average(self, name, val, window=5):
        if name not in self.history:
            self.history[name] = deque(maxlen=window)
        self.history[name].append(val)
        return np.average(self.history[name])
        
    def detect_outliers(self, values, threshold=2.5):
        if len(values) < 3: return [False]*len(values)
        median = np.median(values)
        diff = np.abs(values - median)
        med_abs_deviation = np.median(diff)
        if med_abs_deviation == 0: return [False]*len(values)
        return (0.6745 * diff / med_abs_deviation) > threshold
        
    def kalman_filter_1d(self, measurement, name):
        # Fake Kalman using exponential/moving average per user request flexibility
        return float(self.get_moving_average(name, measurement))

processor = SignalProcessor()

# 7. BIOMECHANICS & GAIT PHASE ==========================================
class BiomechanicsCalculator:
    @staticmethod
    def calc_angle(a, b, c):
        a, b, c = np.array(a), np.array(b), np.array(c)
        rad = np.arctan2(c[1]-b[1], c[0]-b[0]) - np.arctan2(a[1]-b[1], a[0]-b[0])
        ang = np.abs(rad*180.0/np.pi)
        if ang > 180.0: ang = 360 - ang
        return ang

    @staticmethod
    def calculate_foot_progression_angle(heel, ankle, foot_index):
        # FPA (Positivo = toe-out)
        vec = np.array(foot_index) - np.array(heel)
        fpa = np.degrees(np.arctan2(vec[0], -vec[1]))
        return float(fpa)
        
    @staticmethod
    def estimate_arch_height(ankle, foot_index, heel):
        arch_height = ankle[1] - min(foot_index[1], heel[1])
        return abs(float(arch_height))
        
    @staticmethod
    def calculate_symmetry_index(left_val, right_val):
        avg = (left_val + right_val) / 2.0
        if avg == 0: return 100.0
        sym = 100 * (1 - abs(left_val - right_val) / avg)
        return max(0.0, float(sym))

class GaitPhaseDetector:
    def __init__(self):
        self.prev_y = {'R': 0, 'L': 0}
    def detect(self, ankle_r, ankle_l):
        # Detect simple de fase por diferencia en Y
        self.prev_y['R'], self.prev_y['L'] = ankle_r[1], ankle_l[1]
        return "Stance", "Swing"

# 8. MACHINE LEARNING ENGINE ===========================================
import joblib
try:
    from sklearn.ensemble import RandomForestClassifier
    ML_OK = True
except ImportError:
    ML_OK = False

class GaitMLClassifier:
    def __init__(self):
        self.model = None
        self.filename = 'logs/gait_model.pkl'
        self.classes = {
            1: "Marcha Normativa",
            2: "Pronación Dinámica",
            3: "Supinación / Arco Elevado",
            4: "Asimetría Significativa",
            5: "Patrón Compensatorio"
        }
    
    def train_from_buffers(self, features, labels):
        if not ML_OK or len(features) < 15 or len(set(labels)) < 2:
            return False, "Falla cruzada: Insuficientes datos o carece de clases bi-modales"
        try:
            # 11 features expected
            self.model = RandomForestClassifier(n_estimators=100, class_weight='balanced', random_state=42)
            self.model.fit(features, labels)
            joblib.dump(self.model, self.filename)
            return True, "Modelo Re-evaluado y Entrenado Exitosamente"
        except Exception as e:
            return False, str(e)
            
    def predict(self, feature_vector):
        if not self.model: return "Sin IA Clasificadora", 0.0
        try:
            pred = self.model.predict([feature_vector])[0]
            prob = max(self.model.predict_proba([feature_vector])[0]) * 100
            return self.classes.get(pred, "Desconocido"), float(prob)
        except:
            return "Error Inferencial", 0.0

ml_engine = GaitMLClassifier()
if os.path.exists('logs/gait_model.pkl'):
    try:
        if ML_OK: ml_engine.model = joblib.load('logs/gait_model.pkl')
    except: pass

# ======================================================================
# GENERADOR VIDEO CLINICO & UI VISIÓN
# ======================================================================

def safe_camera_loop(source):
    cap = None; tries = 0
    while tries < 5:
        cap = cv2.VideoCapture(source)
        if cap and cap.isOpened(): return cap
        tries += 1; time.sleep(1)
    return None

def generate_clinical_frames(source):
    app_log.info(f"Stream Clínico Solicitado en {source}")
    if str(source).isdigit():
        cap = safe_camera_loop(int(source))
        is_file = False
    else:
        abs_src = os.path.abspath(str(source).replace('\\', '/'))
        cap = safe_camera_loop(abs_src)
        is_file = True

    if not cap: return
    mp_pose = mp.solutions.pose
    mp_drawing = mp.solutions.drawing_utils
    frames_count = 0; start_time = time.time(); fps = 0
    biomech = BiomechanicsCalculator()

    try:
        with mp_pose.Pose(min_detection_confidence=0.5, min_tracking_confidence=0.5) as pose:
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    if is_file: cap.set(cv2.CAP_PROP_POS_FRAMES, 0); continue
                    break

                frames_count += 1
                if frames_count % 30 == 0:
                    fps = 30 / (time.time() - start_time)
                    start_time = time.time()

                if frames_count % 300 == 0:
                    def auto_backup():
                        with state.lock:
                            if state.session_data: pd.DataFrame(list(state.session_data)).to_csv('reports/clinical_auto_backup.csv', index=False)
                    threading.Thread(target=auto_backup, daemon=True).start()

                image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image.flags.writeable = False
                results = pose.process(image)
                image.flags.writeable = True
                image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

                metrics_display = []
                system_status = f"En Parametros | FPS: {fps:.1f} | Pac: {state.current_patient_id or 'Huesped'}"

                if results.pose_landmarks:
                    # Protección contra Landmarks no visibles
                    lms = results.pose_landmarks.landmark
                    key_points = [23, 24, 25, 26, 27, 28, 31, 32]
                    vis = [lms[i].visibility for i in key_points]
                    avg_vis = sum(vis) / len(vis)
                    
                    if avg_vis < 0.5:
                        cv2.putText(image, "VISIBILIDAD BAJA - ACERQUESE", (50, 400), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
                    else:
                        mp_drawing.draw_landmarks(image, results.pose_landmarks, mp_pose.POSE_CONNECTIONS,
                                                  mp_drawing.DrawingSpec(color=(255, 255, 255), thickness=1, circle_radius=1),
                                                  mp_drawing.DrawingSpec(color=(255, 50, 50), thickness=2, circle_radius=1))

                        r_an = [lms[mp_pose.PoseLandmark.RIGHT_ANKLE.value].x, lms[mp_pose.PoseLandmark.RIGHT_ANKLE.value].y]
                        r_ft = [lms[mp_pose.PoseLandmark.RIGHT_FOOT_INDEX.value].x, lms[mp_pose.PoseLandmark.RIGHT_FOOT_INDEX.value].y]
                        r_he = [lms[mp_pose.PoseLandmark.RIGHT_HEEL.value].x, lms[mp_pose.PoseLandmark.RIGHT_HEEL.value].y]
                        l_sh = [lms[mp_pose.PoseLandmark.LEFT_SHOULDER.value].x, lms[mp_pose.PoseLandmark.LEFT_SHOULDER.value].y]
                        l_hp = [lms[mp_pose.PoseLandmark.LEFT_HIP.value].x, lms[mp_pose.PoseLandmark.LEFT_HIP.value].y]
                        l_kn = [lms[mp_pose.PoseLandmark.LEFT_KNEE.value].x, lms[mp_pose.PoseLandmark.LEFT_KNEE.value].y]
                        l_an = [lms[mp_pose.PoseLandmark.LEFT_ANKLE.value].x, lms[mp_pose.PoseLandmark.LEFT_ANKLE.value].y]
                        l_ft = [lms[mp_pose.PoseLandmark.LEFT_FOOT_INDEX.value].x, lms[mp_pose.PoseLandmark.LEFT_FOOT_INDEX.value].y]
                        l_he = [lms[mp_pose.PoseLandmark.LEFT_HEEL.value].x, lms[mp_pose.PoseLandmark.LEFT_HEEL.value].y]
                        
                        # Fix r_sh, r_hp, r_kn references needed for angles
                        r_sh = [lms[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].x, lms[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].y]
                        r_hp = [lms[mp_pose.PoseLandmark.RIGHT_HIP.value].x, lms[mp_pose.PoseLandmark.RIGHT_HIP.value].y]
                        r_kn = [lms[mp_pose.PoseLandmark.RIGHT_KNEE.value].x, lms[mp_pose.PoseLandmark.RIGHT_KNEE.value].y]

                        r_knee_ang = processor.kalman_filter_1d(biomech.calc_angle(r_hp, r_kn, r_an), 'rk')
                        l_knee_ang = processor.kalman_filter_1d(biomech.calc_angle(l_hp, l_kn, l_an), 'lk')
                        r_ankle_ang = processor.kalman_filter_1d(biomech.calc_angle(r_kn, r_an, r_ft), 'ra')
                        l_ankle_ang = processor.kalman_filter_1d(biomech.calc_angle(l_kn, l_an, l_ft), 'la')
                        r_hip_ang = processor.kalman_filter_1d(biomech.calc_angle(r_sh, r_hp, r_kn), 'rh')
                        l_hip_ang = processor.kalman_filter_1d(biomech.calc_angle(l_sh, l_hp, l_kn), 'lh')

                        r_fpa = processor.kalman_filter_1d(biomech.calculate_foot_progression_angle(r_he, r_an, r_ft), 'rfpa')
                        l_fpa = processor.kalman_filter_1d(biomech.calculate_foot_progression_angle(l_he, l_an, l_ft), 'lfpa')
                        r_arch = biomech.estimate_arch_height(r_an, r_ft, r_he)
                        l_arch = biomech.estimate_arch_height(l_an, l_ft, l_he)
                        symmetry = biomech.calculate_symmetry_index(l_knee_ang, r_knee_ang)

                        with state.lock:
                            c_rng = state.clinical_ranges
                            def evt(val, r_key): return c_rng[r_key]['min'] <= val <= c_rng[r_key]['max']
                            
                            t_d_ok, t_i_ok = evt(r_ankle_ang, 'ankle_dorsiflexion'), evt(l_ankle_ang, 'ankle_dorsiflexion')
                            r_d_ok, r_i_ok = evt(r_knee_ang, 'knee_flexion'), evt(l_knee_ang, 'knee_flexion')
                            
                            metrics_display.append(f"Tobillo D: {r_ankle_ang:.1f}   [{'OK' if t_d_ok else 'WR'}]")
                            metrics_display.append(f"Tobillo I: {l_ankle_ang:.1f}   [{'OK' if t_i_ok else 'WR'}]")
                            metrics_display.append(f"Rodilla D: {r_knee_ang:.1f}   [{'OK' if r_d_ok else 'WR'}]")
                            metrics_display.append(f"Rodilla I: {l_knee_ang:.1f}   [{'OK' if r_i_ok else 'WR'}]")
                            metrics_display.append(f"FPA Der  : {r_fpa:+.1f}     [{'OK' if evt(r_fpa, 'foot_progression_angle') else 'WR'}]")
                            
                            pron_text = None
                            if r_fpa > 15 and t_d_ok == False: 
                                pron_text = "PRONACION Dinamica Detectada"
                                voice.alert("Extrema Pronación Visualizada", "high")
                            elif symmetry < 85:
                                pron_text = f"ASIMETRIA MARCH: {symmetry:.0f}%"

                            state.current_live_angles = {
                                'r_ankle': r_ankle_ang, 'l_ankle': l_ankle_ang,
                                'r_knee': r_knee_ang, 'l_knee': l_knee_ang
                            }
                            feature_vector = [r_ankle_ang, l_ankle_ang, r_knee_ang, l_knee_ang, r_hip_ang, l_hip_ang, r_fpa, l_fpa, r_arch, l_arch, symmetry]
                            state.session_data.append(feature_vector)
                            
                        # Dibujador UI
                        cv2.rectangle(image, (10, 10), (330, 20 + len(metrics_display)*25 + (30 if pron_text else 0)), (245, 245, 245), -1)
                        cv2.putText(image, "METRICAS BIOMECANICAS:", (20, 30), cv2.FONT_HERSHEY_DUPLEX, 0.6, (50, 50, 50), 1)
                        for i, m in enumerate(metrics_display):
                            col = (0, 150, 0) if 'OK' in m else (0, 0, 200)
                            cv2.putText(image, m, (20, 55 + i*25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 1, cv2.LINE_AA)
                        if pron_text:
                            yp = 55 + len(metrics_display)*25
                            cv2.rectangle(image, (15, yp-5), (320, yp+20), (0, 200, 255), -1)
                            cv2.putText(image, f"! {pron_text} !", (20, yp+12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 2)
                        
                        ml_class, ml_prob = ml_engine.predict(feature_vector)
                        
                        # Sync to Global State for API
                        with state.lock:
                            state.system_status_msg = f"Auditoría: {ml_class} detectada ({ml_prob:.0f}%) | Paciente: {state.current_patient_id or 'Huesped'}"
                            state.current_live_angles = {
                                'r_ankle': r_ankle_ang, 'l_ankle': l_ankle_ang,
                                'r_knee': r_knee_ang, 'l_knee': l_knee_ang,
                                'symmetry': symmetry
                            }

                        cv2.rectangle(image, (10, image.shape[0]-35), (image.shape[1]-10, image.shape[0]-10), (50, 50, 50), -1)
                        cv2.putText(image, f"{system_status} | IA: {ml_class} ({ml_prob:.0f}%)", (20, image.shape[0]-18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 255, 255), 1)

                ret, buffer = cv2.imencode('.jpg', image)
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
    except Exception as e:
        app_log.error(f"Generador Exception: {e}")
    finally:
        if cap: cap.release()

# ======================================================================
# RUTAS REST API ASISTIA V5
# ======================================================================

@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/video_feed')
def video_feed(): return Response(generate_clinical_frames(request.args.get('src', '0')), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/upload_video', methods=['POST'])
def upload_video():
    if 'video' not in request.files: return jsonify({'status': 'error'})
    file = request.files['video']
    path = os.path.join('uploads', werkzeug.utils.secure_filename(file.filename))
    file.save(path)
    return jsonify({'status': 'ok', 'filepath': path})

@app.route('/api/patient', methods=['POST'])
def create_patient():
    pid = PatientManager.create_patient(request.json)
    with state.lock: state.current_patient_id = pid
    app_log.info(f"Paciente enlazado UUID: {pid}")
    return jsonify({'status': 'ok', 'patient_id': pid})

@app.route('/api/patients', methods=['GET'])
def list_patients():
    ps = []
    for f in os.listdir('patients'):
        if f.endswith('.json'):
            with open(os.path.join('patients', f), 'r') as pfile:
                ps.append(json.load(pfile))
    return jsonify(ps)

@app.route('/api/patient/<pid>', methods=['GET'])
def get_patient(pid):
    p = PatientManager.get_patient(pid)
    return jsonify(p) if p else (jsonify({'error': '404'}), 404)

@app.route('/api/ranges', methods=['GET', 'PUT'])
def clinical_ranges():
    with state.lock:
        if request.method == 'PUT':
            state.clinical_ranges.update(request.json)
            return jsonify({'status': 'ok'})
        return jsonify(state.clinical_ranges)

@app.route('/api/label_sample', methods=['POST'])
def label_sample():
    lbl = request.json.get('label')
    with state.lock:
        if state.session_data:
            state.ml_features_buffer.append(state.session_data[-1])
            state.ml_labels_buffer.append(int(lbl))
            voice.alert("Muestra Guardada", "normal")
            return jsonify({'status': 'ok', 'samples': len(state.ml_labels_buffer)})
    return jsonify({'status': 'error', 'msg': 'Sin cuadros válidos de vision'}), 400

@app.route('/api/train_model', methods=['POST'])
def train_model():
    with state.lock:
        s, msg = ml_engine.train_from_buffers(state.ml_features_buffer, state.ml_labels_buffer)
        if s: voice.alert("Motor Entrenado. Listo.", "high")
    return jsonify({'status': 'ok' if s else 'error', 'msg': msg})

@app.route('/api/status', methods=['GET'])
def sys_status():
    with state.lock:
        return jsonify({'status_msg': state.system_status_msg, 'angles': state.current_live_angles, 'samples': len(state.ml_labels_buffer)})

@app.route('/api/export_report/<pid>', methods=['GET'])
def export_report(pid):
    with state.lock:
        if state.session_data:
            df = pd.DataFrame(list(state.session_data))
            path = f'reports/patient_{pid}_session.csv'
            df.to_csv(path, index=False)
            return jsonify({'status': 'ok', 'file': path})
    return jsonify({'error': 'Sin datos de sesion'})

@app.route('/api/action', methods=['POST'])
def backward_compat_action():
    # Helper to support the old index.html format if the user didn't refresh
    act = request.json.get('action')
    if act in ['1','2','3','4','5']: 
        request.json['label'] = act
        return label_sample()
    elif act == 'train': return train_model()
    return jsonify({'status': 'ok', 'msg':'Compatibilidad enrutada'})

if __name__ == '__main__':
    print(f"DEBUG: Root Path: {app.root_path}")
    print(f"DEBUG: Templates Folder: {app.template_folder}")
    app.run(host='0.0.0.0', port=5001, threaded=True, debug=False)
