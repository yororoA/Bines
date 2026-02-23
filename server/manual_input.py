import zmq
import json
import sys
import time
from pathlib import Path

# 确保可以从项目根目录导入 config
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import ZMQ_HOST, ZMQ_PORTS

# --- Windows Encoding Fix ---
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass
# ----------------------------

PORT = ZMQ_PORTS["MANUAL_TEXT_PUB"]
TOPIC = "text"

def main():
    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.bind(f"tcp://*:{PORT}")
    # socket.bind(f"tcp://{ZMQ_HOST}:{PORT}")
    
    print(f"Manual Input Module Started on Port {PORT}")
    print("Waiting for subscribers to connect...")
    time.sleep(1) # Wait for connection
    
    print("Type your message and press Enter. (Type 'exit' to quit)")
    
    while True:
        try:
            text = input("\n[You]: ").strip()
            if not text: continue
            if text.lower() == "exit": break
            
            payload = {
                "user_input": text,
                "sender": "MANUAL"
            }
            
            # Send: topic="text", payload=json
            socket.send_multipart([TOPIC.encode('utf-8'), json.dumps(payload).encode('utf-8')])
            print(f"[Sent] -> {text}")
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")

    print("Exiting...")
    socket.close()
    context.term()

if __name__ == "__main__":
    main()
