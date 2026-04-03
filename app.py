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
import mimetypes
from logging.handlers import RotatingFileHandler
from collections import deque
from flask import Flask, render_template, Response, jsonify, request, send_file
from functools import wraps
from datetime import datetime
import werkzeug.utils
# from flask_wtf import CSRFProtect
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
import csv

# 1. SETUP DE DIRECTORIOS IMPORTANTES Y GLOBALES ==========================
DIRS = ['uploads', 'reports', 'patients', 'logs']
for d in DIRS:
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB Limit
# csrf = CSRFProtect()
# csrf.init_app(app)

# 1.5. SECURITY: BASIC AUTH ==============================================
def requires_auth(f):
    @wraps(f)
    def decorated(* federal_args, **kwargs):
        auth = request.authorization
        if not auth or not (auth.username == 'admin' and auth.password == 'clinic2026'):
            return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
        return f(*federal_args, **kwargs)
    return decorated

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
        self.ml_features_buffer = deque(maxlen=2000)
        self.ml_labels_buffer = deque(maxlen=2000)
        self.current_live_angles = {}
        self.current_fps = 0
        self.session_active = False
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
                with open(self.buffers_db, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.ml_features_buffer = deque(data.get('features', []), maxlen=2000)
                    self.ml_labels_buffer = deque(data.get('labels', []), maxlen=2000)
            except json.JSONDecodeError as e:
                app_log.error(f"Invalid JSON in {self.buffers_db}: {e}")
            except IOError as e:
                app_log.error(f"Cannot read {self.buffers_db}: {e}")
        if os.path.exists(self.config_db):
            try:
                with open(self.config_db, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.clinical_ranges = data.get('ranges', self.clinical_ranges)
            except json.JSONDecodeError as e:
                app_log.error(f"Invalid JSON in {self.config_db}: {e}")
            except IOError as e:
                app_log.error(f"Cannot read {self.config_db}: {e}")
            
    def save_state(self):
        with self.lock:
            try:
                with open(self.buffers_db, 'w') as f:
                    json.dump({'features': list(self.ml_features_buffer), 'labels': list(self.ml_labels_buffer)}, f)
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
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(patient_data, f,ensure_ascii=False)
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
                app_log.warning(f"TTS Falló al iniciar {e}. El sistema funcionará sin audio.")
                self.engine = None
                return
            while True:
                priority, _, msg = self.queue.get()
                if msg is None: break
                if self.engine:
                    try:
                        self.engine.say(msg)
                        self.engine.runAndWait()
                    except Exception as e:
                        app_log.error(f"TTS error: {e}")
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
        except Exception as e:
            app_log.error(f"ML prediction error: {e}")
            return "Error Inferencial", 0.0

ml_engine = GaitMLClassifier()
if os.path.exists('logs/gait_model.pkl'):
    try:
        if ML_OK: ml_engine.model = joblib.load('logs/gait_model.pkl')
    except Exception as e:
        app_log.error(f"Failed to load ML model: {e}")

# ======================================================================
# GENERADOR VIDEO CLINICO & UI VISIÓN
# ======================================================================

def safe_camera_loop(source):
    app_log.info(f"Attempting to open video source: {source}")
    cap = None; tries = 0
    while tries < 5:
        cap = cv2.VideoCapture(source)
        if cap and cap.isOpened(): 
            app_log.info(f"Successfully opened video source: {source}")
            return cap
        tries += 1; time.sleep(1)
    app_log.error(f"Failed to open video source after 5 tries: {source}")
    return None

# 9. VISION PROCESSING HELPER ===========================================
def process_clinical_frame(frame, pose, biomech, processor, state, voice):
    image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image.flags.writeable = False
    results = pose.process(image)
    image.flags.writeable = True
    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    
    current_angles = {}
    feature_vector = None
    pron_text = None
    metrics_display = []

    if results.pose_landmarks:
        lms = results.pose_landmarks.landmark
        
        # === NUEVO: Validación estricta de landmarks inferiores ===
        # Índices críticos para podiatría: caderas(23,24), rodillas(25,26), tobillos(27,28), pies(29,30,31,32)
        critical_indices = [23, 24, 25, 26, 27, 28, 29, 30, 31, 32]
        critical_vis = [lms[i].visibility for i in critical_indices]
        min_critical_vis = min(critical_vis)
        
        # Si cualquier landmark crítico tiene visibilidad < 0.6, bloqueamos métricas
        if min_critical_vis < 0.6:
            app_log.warning(f"Visibilidad insuficiente ({min_critical_vis:.2f}). Encuadre incorrecto.")
            cv2.putText(image, "🚫 ENCUADRE INCORRECTO - Muestre pies/tobillos", (50, 400), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
            # Devolvemos estado vacío para no contaminar buffers
            return image, {}, None, None

        # Dibujamos landmarks si la visibilidad es OK
        mp.solutions.drawing_utils.draw_landmarks(image, results.pose_landmarks, mp.solutions.pose.POSE_CONNECTIONS,
                                  mp.solutions.drawing_utils.DrawingSpec(color=(255, 255, 255), thickness=1, circle_radius=1),
                                  mp.solutions.drawing_utils.DrawingSpec(color=(255, 50, 50), thickness=2, circle_radius=1))

        def get_cp(lm): return [lm.x, lm.y]
        
        try:
            r_an = get_cp(lms[mp.solutions.pose.PoseLandmark.RIGHT_ANKLE.value])
            r_ft = get_cp(lms[mp.solutions.pose.PoseLandmark.RIGHT_FOOT_INDEX.value])
            r_he = get_cp(lms[mp.solutions.pose.PoseLandmark.RIGHT_HEEL.value])
            l_sh = get_cp(lms[mp.solutions.pose.PoseLandmark.LEFT_SHOULDER.value])
            l_hp = get_cp(lms[mp.solutions.pose.PoseLandmark.LEFT_HIP.value])
            l_kn = get_cp(lms[mp.solutions.pose.PoseLandmark.LEFT_KNEE.value])
            l_an = get_cp(lms[mp.solutions.pose.PoseLandmark.LEFT_ANKLE.value])
            l_ft = get_cp(lms[mp.solutions.pose.PoseLandmark.LEFT_FOOT_INDEX.value])
            l_he = get_cp(lms[mp.solutions.pose.PoseLandmark.LEFT_HEEL.value])
            r_sh = get_cp(lms[mp.solutions.pose.PoseLandmark.RIGHT_SHOULDER.value])
            r_hp = get_cp(lms[mp.solutions.pose.PoseLandmark.RIGHT_HIP.value])
            r_kn = get_cp(lms[mp.solutions.pose.PoseLandmark.RIGHT_KNEE.value])

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

            current_angles = {
                'r_ankle': r_ankle_ang, 'l_ankle': l_ankle_ang,
                'r_knee': r_knee_ang, 'l_knee': l_knee_ang, 'symmetry': symmetry
            }
            feature_vector = [r_ankle_ang, l_ankle_ang, r_knee_ang, l_knee_ang, r_hip_ang, l_hip_ang, r_fpa, l_fpa, r_arch, l_arch, symmetry]

            # Auditoría Clínica UI
            c_rng = state.clinical_ranges
            def evt(val, r_key): return c_rng[r_key]['min'] <= val <= c_rng[r_key]['max']
            t_d_ok = evt(r_ankle_ang, 'ankle_dorsiflexion')
            metrics_display.append(f"Tobillo D: {r_ankle_ang:.1f}   [{'OK' if t_d_ok else 'WR'}]")
            metrics_display.append(f"Tobillo I: {l_ankle_ang:.1f}   [{'OK' if evt(l_ankle_ang, 'ankle_dorsiflexion') else 'WR'}]")
            metrics_display.append(f"Rodilla D: {r_knee_ang:.1f}   [{'OK' if evt(r_knee_ang, 'knee_flexion') else 'WR'}]")
            metrics_display.append(f"Rodilla I: {l_knee_ang:.1f}   [{'OK' if evt(l_knee_ang, 'knee_flexion') else 'WR'}]")
            metrics_display.append(f"FPA Der  : {r_fpa:+.1f}     [{'OK' if evt(r_fpa, 'foot_progression_angle') else 'WR'}]")
            
            if r_fpa > 15 and t_d_ok == False: 
                pron_text = "PRONACION Dinamica Detectada"
                voice.alert("Extrema Pronación Visualizada", "high")
            elif symmetry < 85:
                pron_text = f"ASIMETRIA MARCH: {symmetry:.0f}%"

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
        except Exception as e:
            app_log.warning(f"Error calculando ángulos: {e}")
            current_angles = {}
            feature_vector = None
    return image, current_angles, feature_vector, pron_text

def generate_clinical_frames(source):
    app_log.info(f"Stream Clínico Solicitado en {source}")
    cap = safe_camera_loop(int(source)) if str(source).isdigit() else safe_camera_loop(os.path.abspath(str(source).replace('\\', '/')))
    if not cap: 
        app_log.error(f"Failed to open video source: {source}")
        return

    is_file = not str(source).isdigit()
    mp_pose = mp.solutions.pose
    frames_count = 0; start_time = time.time(); last_backup = time.time()
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
                    with state.lock: state.current_fps = 30 / (time.time() - start_time)
                    start_time = time.time()

                if time.time() - last_backup > 60:
                    last_backup = time.time()
                    with state.lock:
                        if state.session_data: pd.DataFrame(list(state.session_data)).to_csv('reports/clinical_auto_backup.csv', index=False)

                image, angles, features, pron = process_clinical_frame(frame, pose, biomech, processor, state, voice)

                if angles:
                    with state.lock:
                        state.current_live_angles = angles
                        if state.session_active and features is not None:
                            state.session_data.append(features + [state.current_patient_id])
                
                system_status = f"FPS: {state.current_fps:.1f} | Pac: {state.current_patient_id or 'Huesped'}"
                ml_class, ml_prob = ml_engine.predict(features) if features else ("Sin Datos", 0)
                cv2.rectangle(image, (10, image.shape[0]-35), (image.shape[1]-10, image.shape[0]-10), (50, 50, 50), -1)
                cv2.putText(image, f"{system_status} | IA: {ml_class} ({ml_prob:.0f}%)", (20, image.shape[0]-18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 255, 255), 1)

                ret, buffer = cv2.imencode('.jpg', image)
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
    except Exception as e:
        app_log.error(f"Generador Exception: {e}")
    finally:
        if cap: cap.release()

# ======================================================================
# 10. REPORT GENERATOR (FASE 1) ========================================
# ======================================================================

# ======================================================================
# 10. PDF REPORT GENERATOR =============================================
# ======================================================================

def generate_clinical_report_pdf(patient_data, session_metrics, output_path):
    """
    Genera reporte clínico en PDF con métricas de la sesión
    patient_data: dict con info del paciente
    session_metrics: lista de listas con métricas [ra, la, rk, lk, rh, lh, rfpa, lfpa, rarch, larch, sym]
    """
    doc = SimpleDocTemplate(output_path, pagesize=A4,
                          rightMargin=40, leftMargin=40,
                          topMargin=40, bottomMargin=40)
    elements = []
    styles = getSampleStyleSheet()
    
    # Título principal
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'],
                                fontSize=18, spaceAfter=20, alignment=1, textColor=colors.darkblue)
    elements.append(Paragraph("🦶 Reporte de Análisis de Marcha", title_style))
    elements.append(Spacer(1, 10))
    
    # Subtítulo
    elements.append(Paragraph("PodiaAI Clinic v5.0 - Sistema de Evaluación Biomecánica", 
                             ParagraphStyle('Subtitle', parent=styles['Normal'], 
                                          fontSize=10, alignment=1, textColor=colors.grey)))
    elements.append(Spacer(1, 20))
    
    # Datos del paciente
    elements.append(Paragraph("📋 Datos del Paciente", styles['Heading2']))
    patient_info = [
        ['ID Paciente:', patient_data.get('id', 'N/A')],
        ['Nombre:', patient_data.get('name', 'N/A')],
        ['Edad:', str(patient_data.get('age', 'N/A'))],
        ['Sexo:', patient_data.get('sex', 'N/A')],
        ['Tipo de Pie:', patient_data.get('foot_type', 'N/A')],
        ['Fecha de Evaluación:', datetime.now().strftime('%d/%m/%Y %H:%M:%S')]
    ]
    patient_table = Table(patient_info, colWidths=[120, 350])
    patient_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), colors.lightgrey),
        ('GRID', (0,0), (-1,-1), 0.5, colors.black),
        ('PADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    elements.append(patient_table)
    elements.append(Spacer(1, 25))
    
    # Análisis de métricas
    elements.append(Paragraph("📊 Métricas Biomecánicas Promedio", styles['Heading2']))
    
    if session_metrics and len(session_metrics) > 0:
        # Convertir a DataFrame para análisis estadístico
        # Columnas: RA, LA, RK, LK, RH, LH, RFPA, LFPA, RArch, LArch, Sym
        df = pd.DataFrame(session_metrics, 
                         columns=['RA', 'LA', 'RK', 'LK', 'RH', 'LH', 'RFPA', 'LFPA', 'RArch', 'LArch', 'Sym'])
        
        # Calcular estadísticas
        summary = {
            'Tobillo Derecho (°)': f"{df['RA'].mean():.1f} ± {df['RA'].std():.1f}",
            'Tobillo Izquierdo (°)': f"{df['LA'].mean():.1f} ± {df['LA'].std():.1f}",
            'Rodilla Derecho (°)': f"{df['RK'].mean():.1f} ± {df['RK'].std():.1f}",
            'Rodilla Izquierdo (°)': f"{df['LK'].mean():.1f} ± {df['LK'].std():.1f}",
            'Cadera Derecho (°)': f"{df['RH'].mean():.1f} ± {df['RH'].std():.1f}",
            'Cadera Izquierdo (°)': f"{df['LH'].mean():.1f} ± {df['LH'].std():.1f}",
            'FPA Derecho (°)': f"{df['RFPA'].mean():.1f} ± {df['RFPA'].std():.1f}",
            'FPA Izquierdo (°)': f"{df['LFPA'].mean():.1f} ± {df['LFPA'].std():.1f}",
            'Altura Arco Derecho': f"{df['RArch'].mean():.1f} ± {df['RArch'].std():.1f}",
            'Altura Arco Izquierdo': f"{df['LArch'].mean():.1f} ± {df['LArch'].std():.1f}",
            'Índice de Simetría (%)': f"{df['Sym'].mean():.1f} ± {df['Sym'].std():.1f}",
        }
        
        # Crear tabla de métricas
        metrics_data = [['Parámetro', 'Valor Promedio ± DE']]
        for k, v in summary.items():
            metrics_data.append([k, v])
        
        metrics_table = Table(metrics_data, colWidths=[280, 220])
        metrics_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.darkblue),
            ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
            ('GRID', (0,0), (-1,-1), 0.5, colors.black),
            ('PADDING', (0,0), (-1,-1), 8),
            ('ALIGN', (1,1), (-1,-1), 'RIGHT'),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.whitesmoke, colors.white]),
        ]))
        elements.append(metrics_table)
        
        # Análisis de severidad
        elements.append(Spacer(1, 20))
        elements.append(Paragraph("🚦 Evaluación de Severidad", styles['Heading2']))
        
        avg_sym = df['Sym'].mean()
        if avg_sym >= 90:
            severity_color = colors.green
            severity_text = "Leve - Marcha dentro de parámetros normales"
        elif avg_sym >= 75:
            severity_color = colors.orange
            severity_text = "Moderado - Se recomiendan intervenciones"
        else:
            severity_color = colors.red
            severity_text = "Grave - Requiere atención inmediata"
        
        severity_style = ParagraphStyle('Severity', parent=styles['Normal'], 
                                       fontSize=12, textColor=severity_color, spaceAfter=10)
        elements.append(Paragraph(f"Nivel: <b>{severity_text}</b>", severity_style))
        elements.append(Paragraph(f"Índice de Simetría Global: <b>{avg_sym:.1f}%</b>", 
                                 ParagraphStyle('Symmetry', parent=styles['Normal'], fontSize=11)))
        
        # Número de muestras
        elements.append(Spacer(1, 10))
        elements.append(Paragraph(f"Total de muestras analizadas: <b>{len(df)}</b>", 
                                 styles['Normal']))
        
    else:
        elements.append(Paragraph("<i>No hay datos de sesión disponibles para este paciente.</i>", 
                                 styles['Normal']))
    
    # Footer clínico
    elements.append(Spacer(1, 40))
    disclaimer = Paragraph(
        "<b>Nota Clínica:</b> Este reporte es una herramienta de apoyo a la decisión clínica. "
        "Los valores deben interpretarse en contexto con la evaluación física completa del paciente. "
        "Rangos de referencia basados en literatura biomecánica estándar (Perry & Burnfield, Gait Analysis, 2nd ed.). "
        "<br/><br/><i>PodiaAI Clinic v5.0 - Sistema de Análisis de Marcha Asistido por IA</i>",
        ParagraphStyle('Disclaimer', fontSize=8, textColor=colors.grey, spaceBefore=10)
    )
    elements.append(disclaimer)
    
    # Generar PDF
    try:
        doc.build(elements)
        return True
    except Exception as e:
        app_log.error(f"Error generando PDF: {e}")
        return False


# ======================================================================
# RUTAS REST API ASISTIA V5
# ======================================================================

@app.route('/')
@requires_auth
def index():
    return render_template('dashboard.html')

@app.route('/video_feed')
@requires_auth
def video_feed(): return Response(generate_clinical_frames(request.args.get('src', '0')), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/upload_video', methods=['POST'])
@requires_auth
def upload_video():
    if 'video' not in request.files: return jsonify({'status': 'error', 'msg': 'No file'})
    file = request.files['video']
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    if ext not in {'mp4', 'avi', 'mov', 'mkv', 'webm'}: return jsonify({'status': 'error', 'msg': 'Invalid format'})
    
    path = os.path.join('uploads', werkzeug.utils.secure_filename(file.filename))
    file.save(path)
    
    # Check duration (max 5 min)
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    if fps > 0 and (frames/fps) > 300: 
        os.remove(path)
        return jsonify({'status': 'error', 'msg': 'Video too long (max 5 min)'})
    
    return jsonify({'status': 'ok', 'filepath': path})

@app.route('/api/patient', methods=['POST'])
@requires_auth
def create_patient():
    pid = PatientManager.create_patient(request.json)
    with state.lock: state.current_patient_id = pid
    return jsonify({'status': 'ok', 'patient_id': pid})

@app.route('/api/patient/active/<pid>', methods=['PUT'])
@requires_auth
def set_active_patient(pid):
    p = PatientManager.get_patient(pid)
    if p:
        with state.lock: state.current_patient_id = pid
        return jsonify({'status': 'ok', 'name': p['name']})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/session/toggle', methods=['POST'])
@requires_auth
def toggle_session_v5():
    with state.lock: 
        state.session_active = not state.session_active
        if state.session_active:
            state.session_data = [] # Reset buffers on new session
        return jsonify({'status': 'ok', 'active': state.session_active})

@app.route('/api/export_report/<pid>', methods=['GET'])
@requires_auth
def export_report_v5(pid):
    with state.lock:
        if state.session_data:
            # Filtrar por paciente si el index 11 coincide (agregado en generate_frames)
            p_data = [d[:11] for d in state.session_data if len(d) > 11 and d[11] == pid]
            if not p_data: p_data = [d[:11] for d in state.session_data] # Fallback
            df = pd.DataFrame(p_data, columns=['RA', 'LA', 'RK', 'LK', 'RH', 'LH', 'RFPA', 'LFPA', 'RArch', 'LArch', 'Sym'])
            path = f'reports/patient_{pid}_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
            df.to_csv(path, index=False)
            return send_file(os.path.abspath(path), as_attachment=True)
    return jsonify({'error': 'No data'}), 400

@app.route('/api/status', methods=['GET'])
@requires_auth
def sys_status():
    with state.lock:
        return jsonify({
            'status_msg': state.system_status_msg, 
            'angles': state.current_live_angles, 
            'samples': len(state.ml_labels_buffer),
            'fps': round(state.current_fps, 1),
            'session_active': state.session_active,
            'current_patient': state.current_patient_id
        })

@app.route('/api/patients', methods=['GET'])
@requires_auth
def list_patients():
    ps = []
    if os.path.exists('patients'):
        for f in os.listdir('patients'):
            if f.endswith('.json'):
                try:
                    # Se agrega encoding='utf-8' para evitar errores con caracteres especiales
                    with open(os.path.join('patients', f), 'r', encoding='utf-8') as pfile: 
                        ps.append(json.load(pfile))
                except Exception as e:
                    app_log.error(f"Error leyendo el paciente {f}: {e}")
    return jsonify(ps)

@app.route('/api/ranges', methods=['GET', 'PUT'])
@requires_auth
def clinical_ranges():
    with state.lock:
        if request.method == 'PUT':
            state.clinical_ranges.update(request.json)
            state.save_state()
            return jsonify({'status': 'ok'})
        return jsonify(state.clinical_ranges)

@app.route('/api/label_sample', methods=['POST'])
@requires_auth
def label_sample():
    label = request.json.get('label')
    if not label: return jsonify({'error': 'No label provided'}), 400
    
    with state.lock:
        # Usamos el último vector de características recolectado en la sesión
        if not state.session_data:
            return jsonify({'error': 'No hay datos de sesión para etiquetar. Inicie captura primero.'}), 400
            
        last_features = state.session_data[-1][:11]
        
        csv_file = 'logs/gait_training_data.csv'
        header = ['ra', 'la', 'rk', 'lk', 'rh', 'lh', 'rfpa', 'lfpa', 'rarch', 'larch', 'sym', 'label']
        file_exists = os.path.isfile(csv_file)
        
        with open(csv_file, 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists: writer.writerow(header)
            writer.writerow(list(last_features) + [label])
            
    app_log.info(f"Muestra etiquetada: {label}")
    return jsonify({'status': 'ok', 'msg': f'Muestra guardada como {label}'})

@app.route('/api/train_model', methods=['POST'])
@requires_auth
def train_model():
    csv_file = 'logs/gait_training_data.csv'
    if not os.path.exists(csv_file):
        return jsonify({'error': 'No hay base de datos de entrenamiento (logs/gait_training_data.csv)'}), 400
        
    try:
        df = pd.read_csv(csv_file)
        if len(df) < 10:
            return jsonify({'error': f'Insuficientes muestras ({len(df)}/10)'}), 400
            
        X = df.iloc[:, :-1].values
        y = df.iloc[:, -1].values
        
        # Mapa de etiquetas para el RandomForest
        mapping = {"Normal": 1, "Pronador": 2, "Supinador": 3, "Asimétrico": 4}
        y_encoded = [mapping.get(l, 5) for l in y]
        
        ok, msg = ml_engine.train_from_buffers(X, y_encoded)
        return jsonify({'status': 'ok' if ok else 'error', 'msg': msg, 'samples': len(df)})
    except Exception as e:
        app_log.error(f"Training error: {e}")
        return jsonify({'error': str(e)}), 500

# ======================================================================
# RUTAS ADICIONALES PARA REPORTES PDF
# ======================================================================

@app.route('/api/report/generate/<pid>', methods=['POST'])
@requires_auth
def generate_report(pid):
    """Genera reporte PDF para un paciente con datos de sesión"""
    app_log.info(f"Solicitando generar reporte para paciente: {pid}")
    
    # Obtener datos del paciente
    patient = PatientManager.get_patient(pid)
    if not patient:
        app_log.error(f"Paciente {pid} no encontrado")
        return jsonify({'error': 'Paciente no encontrado'}), 404
    
    # Obtener métricas de sesión del paciente
    with state.lock:
        # Filtrar métricas del paciente específico
        session_data = list(state.session_data)
        patient_metrics = [d[:11] for d in session_data if len(d) > 11 and d[11] == pid]
        
        # Fallback: si no hay datos específicos del paciente, usar todos
        if not patient_metrics:
            app_log.warning(f"No hay datos específicos para paciente {pid}, usando todos los datos de sesión")
            patient_metrics = [d[:11] for d in session_data]
    
    app_log.info(f"Generando PDF con {len(patient_metrics)} muestras")
    
    # Generar nombre de archivo único
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"reporte_{pid}_{timestamp}.pdf"
    output_path = os.path.join('reports', filename)
    
    # Generar PDF
    try:
        success = generate_clinical_report_pdf(patient, patient_metrics, output_path)
        if success:
            app_log.info(f"Reporte generado exitosamente: {output_path}")
            return jsonify({
                'status': 'ok', 
                'report_url': f'/api/report/download/{filename}',
                'filename': filename,
                'samples': len(patient_metrics)
            })
        else:
            return jsonify({'error': 'Error al generar el PDF'}), 500
    except Exception as e:
        app_log.error(f"Error generando reporte: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/report/download/<filename>', methods=['GET'])
@requires_auth
def download_report(filename):
    """Descarga reporte PDF generado"""
    filepath = os.path.join('reports', filename)
    if os.path.exists(filepath):
        app_log.info(f"Descargando reporte: {filepath}")
        return send_file(filepath, as_attachment=True, download_name=filename)
    app_log.error(f"Archivo no encontrado: {filepath}")
    return jsonify({'error': 'Archivo no encontrado'}), 404

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001, threaded=True, debug=False)
