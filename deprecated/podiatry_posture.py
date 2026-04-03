import cv2
import mediapipe as mp
import numpy as np
import pyttsx3
import pandas as pd
import time
import queue
import threading
import os
from datetime import datetime

try:
    from sklearn.ensemble import RandomForestClassifier
    import joblib
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    print("scikit-learn o joblib no están instalados. El clasificador no estará disponible.")

# ---------------------------------------------------------
# CONFIGURACIÓN DE VOZ (TTS) EN HILO SEPARADO
# ---------------------------------------------------------
# Se utiliza un hilo para evitar que pyttsx3 congele OpenCV
voice_queue = queue.Queue()

def voice_worker():
    """Hilo dedicado para reproducir el texto a voz sin bloquear el loop de video."""
    import pythoncom
    pythoncom.CoInitialize() # Necesario en Windows para inicializar COM en un hilo
    engine = pyttsx3.init()
    engine.setProperty('rate', 150) # Velocidad del habla
    
    while True:
        msg = voice_queue.get()
        if msg is None: # Señal de salida
            break
        engine.say(msg)
        engine.runAndWait()
        voice_queue.task_done()

# Iniciar hilo de voz en background
voice_thread = threading.Thread(target=voice_worker, daemon=True)
voice_thread.start()

last_voice_time = 0
def speak_async(message):
    """Envía un mensaje a la cola de voz cuidando no saturar auditivamente al usuario."""
    global last_voice_time
    current_time = time.time()
    # Limitar las alertas habladas a 1 cada X segundos para no saturar
    if current_time - last_voice_time > 4:
        voice_queue.put(message)
        last_voice_time = current_time

# ---------------------------------------------------------
# CÁLCULOS GEOMÉTRICOS Y RANGOS
# ---------------------------------------------------------
def calculate_angle(a, b, c):
    """
    Calcula el ángulo entre 3 puntos (a, b, c). 
    'b' corresponde a la articulación en mediapipe (ej. rodilla).
    """
    a = np.array(a) # Primer punto (ej. cadera)
    b = np.array(b) # Vértice     (ej. rodilla)
    c = np.array(c) # Segundo punto (ej. tobillo)
    
    radians = np.arctan2(c[1]-b[1], c[0]-b[0]) - np.arctan2(a[1]-b[1], a[0]-b[0])
    angle = np.abs(radians*180.0/np.pi)
    
    # Nos aseguramos que el ángulo esté dentro del límite [0, 180]
    if angle > 180.0:
        angle = 360 - angle
        
    return angle

# Rangos normales configurables: [Mínimo, Máximo] grados
RANGOS_NORMALES = {
    'tobillo': [70, 110],  # Ángulo: Rodilla -> Tobillo -> Punta del pie
    'rodilla': [160, 180], # Ángulo: Cadera -> Rodilla -> Tobillo
    'cadera':  [160, 180]  # Ángulo: Hombro -> Cadera -> Rodilla
}

def verify_angle(angle, angle_type):
    """Verifica si un ángulo calculado está dentro del rango esperado."""
    rmin, rmax = RANGOS_NORMALES[angle_type]
    return rmin <= angle <= rmax

def draw_text(img, text, pos, color=(255, 255, 255), scale=0.6, thickness=1):
    """Dibuja un texto en pantalla con estilo OpenCV."""
    cv2.putText(img, text, pos, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)

# ---------------------------------------------------------
# BUCLE PRINCIPAL DE LA APLICACIÓN
# ---------------------------------------------------------
def main():
    # Inicializar componentes de MediaPipe usando importaciones estándar
    mp_pose = mp.solutions.pose
    mp_drawing = mp.solutions.drawing_utils
    
    cap = cv2.VideoCapture(0) # Iniciar Webcam
    
    # Variables de Control y Logging
    session_data = []
    out_of_range_counters = {'tobillo': 0, 'rodilla': 0, 'cadera': 0}
    total_frames = 0
    start_time = datetime.now()

    # Variables para Clasificador ML (Entrenamiento dinámico)
    features_buffer = [] # Matriz X
    labels_buffer = []   # Vector y
    model_filename = 'posture_model.pkl'
    classifier = None
    
    # Si ya existe un modelo previo entrenado, intentamos cargarlo
    if ML_AVAILABLE and os.path.exists(model_filename):
        try:
            classifier = joblib.load(model_filename)
            print(f"[!] Modelo ML {model_filename} cargado correctamente.")
        except Exception as e:
            print(f"[ERROR] No se pudo cargar el modelo: {e}")

    # Instrucciones por consola
    print("\n" + "="*40)
    print("SISTEMA DE POSTURA PARA PODIATRÍA INICIADO")
    print("="*40)
    print("CONTROLES:")
    print("  [s] -> Guardar sesión en CSV (Data Logging)")
    print("  [1] -> IA: Etiquetar postura como 1 (Normal)")
    print("  [2] -> IA: Etiquetar postura como 2 (Pronación/Supinación)")
    print("  [3] -> IA: Etiquetar postura como 3 (Valgo/Inestable)")
    print("  [t] -> IA: Entrenar y Guardar modelo (Min. 10 muestras)")
    print("  [r] -> Reiniciar tracking temporal")
    print("  [q] -> Salir y Generar resumen")
    print("="*40 + "\n")

    # Iniciar pipeline de MediaPipe
    with mp_pose.Pose(min_detection_confidence=0.5, min_tracking_confidence=0.5) as pose:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
                
            total_frames += 1
            current_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Convertir colores OpenCV a MediaPipe
            image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image.flags.writeable = False
            results = pose.process(image)
            
            # Revertir a colores listos para dibujar
            image.flags.writeable = True
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

            angles = {}
            status_msgs = []
            alert_voice_msgs = []

            # Si encontramos esqueleto
            if results.pose_landmarks:
                # Dibujar esqueleto general (opcional, para visualización rápida)
                # mp_drawing.draw_landmarks(image, results.pose_landmarks, mp_pose.POSE_CONNECTIONS)
                
                landmarks = results.pose_landmarks.landmark

                try:
                    # OBTENCIÓN DE PUNTOS CLAVE (Coordenadas X, Y)
                    # Pierna Derecha
                    r_shoulder = [landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].x, landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].y]
                    r_hip = [landmarks[mp_pose.PoseLandmark.RIGHT_HIP.value].x, landmarks[mp_pose.PoseLandmark.RIGHT_HIP.value].y]
                    r_knee = [landmarks[mp_pose.PoseLandmark.RIGHT_KNEE.value].x, landmarks[mp_pose.PoseLandmark.RIGHT_KNEE.value].y]
                    r_ankle = [landmarks[mp_pose.PoseLandmark.RIGHT_ANKLE.value].x, landmarks[mp_pose.PoseLandmark.RIGHT_ANKLE.value].y]
                    r_foot = [landmarks[mp_pose.PoseLandmark.RIGHT_FOOT_INDEX.value].x, landmarks[mp_pose.PoseLandmark.RIGHT_FOOT_INDEX.value].y]

                    # Pierna Izquierda
                    l_shoulder = [landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER.value].x, landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER.value].y]
                    l_hip = [landmarks[mp_pose.PoseLandmark.LEFT_HIP.value].x, landmarks[mp_pose.PoseLandmark.LEFT_HIP.value].y]
                    l_knee = [landmarks[mp_pose.PoseLandmark.LEFT_KNEE.value].x, landmarks[mp_pose.PoseLandmark.LEFT_KNEE.value].y]
                    l_ankle = [landmarks[mp_pose.PoseLandmark.LEFT_ANKLE.value].x, landmarks[mp_pose.PoseLandmark.LEFT_ANKLE.value].y]
                    l_foot = [landmarks[mp_pose.PoseLandmark.LEFT_FOOT_INDEX.value].x, landmarks[mp_pose.PoseLandmark.LEFT_FOOT_INDEX.value].y]

                    # CÁLCULO DE ÁNGULOS EN TIEMPO REAL
                    angles['r_cadera'] = calculate_angle(r_shoulder, r_hip, r_knee)
                    angles['l_cadera'] = calculate_angle(l_shoulder, l_hip, l_knee)
                    
                    angles['r_rodilla'] = calculate_angle(r_hip, r_knee, r_ankle)
                    angles['l_rodilla'] = calculate_angle(l_hip, l_knee, l_ankle)
                    
                    angles['r_tobillo'] = calculate_angle(r_knee, r_ankle, r_foot)
                    angles['l_tobillo'] = calculate_angle(l_knee, l_ankle, l_foot)

                    # RUTINAS DE ALERTA Y FEEDBACK
                    def check_and_alert(val, name, a_type):
                        if not verify_angle(val, a_type):
                            out_of_range_counters[a_type] += 1
                            # Devuelve el texto visual y la instrucción de voz
                            return f"(!) {name} alerta: {int(val)} g.", f"Ajusta el ángulo del {a_type}"
                        return f"(ok) {name}: {int(val)} g.", None

                    r_ankle_msg, v1 = check_and_alert(angles['r_tobillo'], 'T.Der', 'tobillo')
                    l_ankle_msg, v2 = check_and_alert(angles['l_tobillo'], 'T.Izq', 'tobillo')
                    r_knee_msg, v3 = check_and_alert(angles['r_rodilla'], 'R.Der', 'rodilla')
                    l_knee_msg, v4 = check_and_alert(angles['l_rodilla'], 'R.Izq', 'rodilla')
                    r_hip_msg, v5 = check_and_alert(angles['r_cadera'], 'C.Der', 'cadera')
                    l_hip_msg, v6 = check_and_alert(angles['l_cadera'], 'C.Izq', 'cadera')

                    # Encolamos advertencias de voz dando prioridad inferior y rodilla
                    for m in [v1, v2, v3, v4, v5, v6]:
                        if m: alert_voice_msgs.append(m)

                    status_msgs = [r_ankle_msg, l_ankle_msg, r_knee_msg, l_knee_msg, r_hip_msg, l_hip_msg]

                    todas_normales = len(alert_voice_msgs) == 0
                    postura_str = "Correcta" if todas_normales else "Incorrecta"
                    
                    # LOGGING CONTINUO
                    session_data.append({
                        'timestamp': current_timestamp,
                        'r_cadera': round(angles['r_cadera'], 2), 'l_cadera': round(angles['l_cadera'], 2),
                        'r_rodilla': round(angles['r_rodilla'],2), 'l_rodilla': round(angles['l_rodilla'],2),
                        'r_tobillo': round(angles['r_tobillo'],2), 'l_tobillo': round(angles['l_tobillo'],2),
                        'reglas_postura': postura_str
                    })

                    # CONFIGURACIÓN CLASIFICADOR IA
                    # Construir array con las features (X) del frame:
                    current_features = [
                        angles['r_tobillo'], angles['l_tobillo'],
                        angles['r_rodilla'], angles['l_rodilla'],
                        angles['r_cadera'], angles['l_cadera']
                    ]

                    # Si el modelo está activo, lo predecimos en tiempo real
                    if classifier:
                        pred = classifier.predict([current_features])[0]
                        pred_class = ""
                        if pred == 1: pred_class = "[IA] Postura Saludable"
                        elif pred == 2: pred_class = "[IA] Pronación/Supinación Detectada"
                        elif pred == 3: pred_class = "[IA] Rodilla Inestable (Valgo)"
                        draw_text(image, pred_class, (10, 240), (0, 255, 255), 0.7, 2)

                except Exception as e:
                    # En caso de que se pierdan las landmarks, evitar que la app crashee
                    pass

            # ==============================
            # UI VISUAL SOBRE LA IMAGEN
            # ==============================
            # Crear un overlay oscuro para el texto
            overlay = image.copy()
            cv2.rectangle(overlay, (0, 0), (280, 220), (0, 0, 0), -1)
            cv2.addWeighted(overlay, 0.6, image, 0.4, 0, image)

            draw_text(image, "MONITOR PODIATRICO", (10, 20), (255, 255, 0), 0.6, 2)
            
            y_offset = 50
            for msg in status_msgs:
                # Color Verde si (ok), Rojo si (!)
                color = (0, 255, 0) if "(ok)" in msg else (0, 0, 255)
                draw_text(image, msg, (10, y_offset), color)
                y_offset += 25

            # ==============================
            # FEEDBACK AUDITIVO (VOZ)
            # ==============================
            if alert_voice_msgs:
                # Reproducimos el primer error encontrado
                speak_async(alert_voice_msgs[0])

            cv2.imshow('Podiatry IA Posture', image)

            # --- MANEJO DE TECLAS Y EVENTOS ---
            key = cv2.waitKey(10) & 0xFF
            
            if key == ord('q'):
                break
                
            elif key == ord('s'):
                df = pd.DataFrame(session_data)
                filename = f"sesion_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                df.to_csv(filename, index=False)
                speak_async("Sesión de datos guardada en Excel")
                print(f"[INFO] Log guardado exitosamente en: {filename}")
                
            # Etiquetar con Clasificador ML en vivo
            elif key in [ord('1'), ord('2'), ord('3')]:
                if results.pose_landmarks and len(angles) == 6:
                    label = int(chr(key))
                    features_buffer.append(current_features)
                    labels_buffer.append(label)
                    speak_async(f"Muestra {label} registrada")
                    print(f"[ML] Captura -> Label: {label} guardada.")
                    
            elif key == ord('t'):
                if ML_AVAILABLE and len(features_buffer) >= 10:
                    print("[INFO] Entrenando modelo RandomForest con muestras capturadas...")
                    clf = RandomForestClassifier(n_estimators=50, max_depth=5, random_state=42)
                    clf.fit(features_buffer, labels_buffer)
                    joblib.dump(clf, model_filename)
                    classifier = clf
                    speak_async("El modelo se ha entrenado satisfactoriamente")
                    print(f"[V] Modelo entrenado y activado. Archivo: {model_filename}")
                else:
                    msg = "Hacen falta al menos 10 muestras con 1, 2, o 3."
                    print(f"[Alerta] {msg}")
                    speak_async("Faltan datos para entrenar el modelo")

            elif key == ord('r'):
                print("Reiniciando datos visuales temporales.")

    # --- REPORTE DE RESUMEN FINAL AL CERRAR ---
    cap.release()
    cv2.destroyAllWindows()
    
    print("\n" + "="*50)
    print(" INFORME FINAL DE SESIÓN PODIÁTRICA")
    print("="*50)
    print(f" > Duración evaluada: {datetime.now() - start_time}")
    print(f" > Frames procesados: {total_frames}")
    if total_frames > 0:
        for angle_type, count in out_of_range_counters.items():
            porcentaje = (count / total_frames) * 100
            if porcentaje > 0:
                print(f"  - Desviación en {angle_type.upper()}: Detectada en el {porcentaje:.2f}% del tiempo.")
    print("="*50 + "\n")
    
    # Detener hilo de voz
    voice_queue.put(None)

if __name__ == '__main__':
    main()
