import cv2
import json
import time

def calibrate_camera():
    print("=== ASISTIA V5: RUTINA DE CALIBRACIÓN DE CÁMARA ===")
    print("Iniciando busqueda de puertos de hardware OpenCV...")
    
    available_cams = []
    for i in range(5):
        cap = cv2.VideoCapture(i)
        if cap and cap.isOpened():
            ret, frame = cap.read()
            if ret:
                h, w, fps = frame.shape[0], frame.shape[1], cap.get(cv2.CAP_PROP_FPS)
                available_cams.append({'puerto': i, 'res': f"{w}x{h}", 'fps': fps})
            cap.release()
            
    if not available_cams:
        print("[!] FATAL: No se detectaron cámaras en Windows.")
        return
        
    print("\nCámaras detectadas:")
    for c in available_cams:
        print(f" -> Puerto {c['puerto']} | Resolucion: {c['res']} | Max FPS: {c['fps']}")
        
    try:
        seleccion = int(input("\nSeleccione el puerto clínico preferido (ej: 0): "))
        
        print("\nAbriendo camara... Presione la tecla 'Q' para finalizar y guardar config.")
        cap = cv2.VideoCapture(seleccion)
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret: break
            
            # Dibujar rectangulo de zona verde central
            h, w = frame.shape[:2]
            cv2.rectangle(frame, (int(w*0.2), int(h*0.1)), (int(w*0.8), int(h*0.9)), (0, 255, 0), 2)
            cv2.putText(frame, "ZONA NORMAL DE MARCHA", (int(w*0.2), int(h*0.08)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            
            cv2.imshow("AsistIA Calibracion", frame)
            
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
                
        cap.release()
        cv2.destroyAllWindows()
        
        config = {"default_camera_port": seleccion, "calibrated_at": time.time()}
        with open("config_clinic.json", "w") as f:
            json.dump(config, f)
            
        print("\n[✓] Calibración guardada en config_clinic.json exitosamente.")
        
    except ValueError:
        print("[!] Input inválido. Operación cancelada.")

if __name__ == "__main__":
    calibrate_camera()
