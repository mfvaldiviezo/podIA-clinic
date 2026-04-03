# AsistIA V5 - PodiaAI Clinic

Sistema profesional de análisis biomecánico y monitoreo de postura podológica.

## Descripción
AsistIA V5 es una aplicación clínica diseñada para la captura y análisis en tiempo real de la marcha y la postura. Utiliza visión por computadora (MediaPipe) e inteligencia artificial para extraer métricas biomécanicas precisas, ayudando en el diagnóstico y seguimiento de pacientes.

## Características Principales
- **Análisis en Tiempo Real:** Monitoreo de ángulos de tobillo, rodilla y cadera.
- **Detección de Patologías:** Identificación dinámica de pronación, supinación y asimetrías.
- **Gestión de Pacientes:** Base de datos local para historial clínico y sesiones.
- **Inteligencia Artificial:** Clasificador de marcha basado en Random Forest (Scikit-Learn).
- **Reportes:** Exportación de datos de sesión en formato CSV.
- **Alertas por Voz:** Retroalimentación auditiva inmediata durante la evaluación.

## Requisitos
- Python 3.8+
- OpenCV
- MediaPipe
- Flask
- Pandas / NumPy
- Scikit-Learn
- Pyttsx3

## Instalación
1. Clonar el repositorio.
2. Instalar dependencias:
   ```bash
   pip install -r requirements.txt
   ```
3. Ejecutar la aplicación:
   ```bash
   python app.py
   ```
4. Acceder vía navegador a `http://127.0.0.1:5001`.

## Estructura del Proyecto
- `app.py`: Servidor principal y lógica de negocio.
- `templates/`: Interfaces de usuario (HTML).
- `static/`: Recursos estáticos (JS, CSS, Imágenes).
- `patients/`: Datos de pacientes (Ignorado por Git).
- `reports/`: Reportes generados (Ignorado por Git).
- `uploads/`: Videos cargados para análisis (Ignorado por Git).
- `logs/`: Registros del sistema y modelos de IA.

---
*Desarrollado para Proyecto IA UNL*
