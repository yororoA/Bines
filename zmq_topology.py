from config import ZMQ_PORTS


# 服务名称到「本服务 bind 的端口」映射（单一来源）
SERVICE_BIND_PORTS = {
    "Display": [
        ZMQ_PORTS["CONTROL_PUB"],
    ],
    "Speaking": [
        ZMQ_PORTS["TTS_AUDIO_PUB"],
    ],
    "Visual": [
        ZMQ_PORTS["VISUAL_PUB"],
        ZMQ_PORTS["VISUAL_REQREP"],
    ],
    "RAG Server": [
        ZMQ_PORTS["RAG_SERVER_REQREP"],
    ],
    "Thinking": [
        ZMQ_PORTS["THINKING_TTS_PUB"],
        ZMQ_PORTS["THINKING_TEXT_PUB"],
        ZMQ_PORTS["CONTROL_PUB_THINKING"],
        ZMQ_PORTS["START_THINKING_REP"],
    ],
    "Classification": [
        ZMQ_PORTS["CLASSIFICATION_PUB"],
        ZMQ_PORTS["MODULE_READY_REP"],
    ],
    "Hearing": [
        ZMQ_PORTS["HEARING_ASR_PUB"],
    ],
    "Bored Detector": [
        ZMQ_PORTS["BORED_PUB"],
    ],
    "ChatBot": [
        ZMQ_PORTS["QQ_PUB"],
    ],
}


def get_service_bind_ports(service_name: str):
    return SERVICE_BIND_PORTS.get(service_name, [])
