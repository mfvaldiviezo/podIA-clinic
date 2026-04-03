import sys
import traceback
print("Python Path:", sys.executable)
try:
    import mediapipe as mp
    print("MediaPipe version:", getattr(mp, '__version__', 'unknown'))
    print("Does mp have solutions?", hasattr(mp, 'solutions'))
except Exception as e:
    print("Failed to import mediapipe as mp:", e)

try:
    import mediapipe.solutions.pose as mp_pose
    print("Successfully imported mediapipe.solutions.pose")
    print(dir(mp_pose))
except Exception as e:
    print("Failed to import mediapipe.solutions.pose")
    traceback.print_exc()

try:
    from mediapipe.tasks import python as mp_tasks
    from mediapipe.tasks.python import vision
    print("Tasks vision successfully imported (New API usage if needed).")
except Exception as e:
    print("Failed to import tasks API:", e)
