# -*- coding: utf-8 -*-
"""
统一通信协议模块
定义系统中所有模块间通信的消息格式和协议规范
"""

import json
import time
import hashlib
import uuid
import zmq

# 模块类型定义
MODULE_TYPES = {
    "GUI": "gui",
    "HEARING": "hearing",
    "VISUAL": "visual",
    "THINKING": "thinking",
    "SPEAKING": "speaking",
    "CLASSIFICATION": "classification"
}

# 消息类型定义
MESSAGE_TYPES = {
    "REQUEST": "request",
    "RESPONSE": "response",
    "NOTIFICATION": "notification",
    "CONTROL": "control",
    "DATA": "data"
}

# 消息主题定义
TOPICS = {
    "ALL": "all",
    "CONTROL": "control",
    "ASR": "asr",
    "TTS": "tts",
    "VISUAL": "visual",
    "THINK": "think",
    "CLASSIFICATION": "classification",
    "DISCOVERY": "discovery"
}

class MessageFormatError(Exception):
    """消息格式错误"""
    pass

class MessageVerificationError(Exception):
    """消息验证错误"""
    pass

class CommunicationProtocol:
    """统一通信协议类"""
    
    @staticmethod
    def create_message(
        message_type,
        module_type,
        content=None,
        request_id=None,
        topic="all",
        source="unknown",
        target="all",
        priority="normal",
        timestamp=None
    ):
        """
        创建统一格式的消息
        
        Args:
            message_type: 消息类型（request/response/notification/control/data）
            module_type: 模块类型
            content: 消息内容
            request_id: 请求ID（用于响应消息关联）
            topic: 消息主题
            source: 消息源
            target: 消息目标
            priority: 优先级（high/normal/low）
            timestamp: 时间戳
            
        Returns:
            dict: 统一格式的消息
        """
        if message_type not in MESSAGE_TYPES.values():
            raise MessageFormatError(f"Invalid message type: {message_type}")
        
        if module_type not in MODULE_TYPES.values():
            raise MessageFormatError(f"Invalid module type: {module_type}")
        
        if topic not in TOPICS.values():
            raise MessageFormatError(f"Invalid topic: {topic}")
        
        # 生成唯一消息ID
        message_id = str(uuid.uuid4())
        
        # 使用当前时间戳
        if timestamp is None:
            timestamp = int(time.time() * 1000)  # 毫秒级时间戳
        
        # 构建消息
        message = {
            "header": {
                "message_id": message_id,
                "request_id": request_id,
                "message_type": message_type,
                "module_type": module_type,
                "topic": topic,
                "source": source,
                "target": target,
                "priority": priority,
                "timestamp": timestamp,
                "version": "1.0"  # 协议版本
            },
            "body": content or {}
        }
        
        # 添加签名
        message["signature"] = CommunicationProtocol._generate_signature(message)
        
        return message
    
    @staticmethod
    def _generate_signature(message):
        """
        生成消息签名
        
        Args:
            message: 消息字典
            
        Returns:
            str: 消息签名
        """
        # 只对header和body进行签名
        data_to_sign = {
            "header": message["header"],
            "body": message["body"]
        }
        
        # 转换为JSON字符串，确保顺序一致
        json_str = json.dumps(data_to_sign, sort_keys=True, ensure_ascii=False)
        
        # 使用SHA-256生成签名
        signature = hashlib.sha256(json_str.encode('utf-8')).hexdigest()
        
        return signature
    
    @staticmethod
    def verify_message(message):
        """
        验证消息的完整性和真实性
        
        Args:
            message: 消息字典
            
        Returns:
            bool: 验证结果
        """
        # 检查消息格式
        if not isinstance(message, dict):
            raise MessageFormatError("Message must be a dictionary")
        
        # 检查必要字段
        if "header" not in message or "body" not in message or "signature" not in message:
            raise MessageFormatError("Message missing required fields")
        
        # 生成验证签名
        verification_signature = CommunicationProtocol._generate_signature(message)
        
        # 验证签名
        if message["signature"] != verification_signature:
            raise MessageVerificationError("Invalid message signature")
        
        return True
    
    @staticmethod
    def serialize_message(message):
        """
        序列化消息
        
        Args:
            message: 消息字典
            
        Returns:
            bytes: 序列化后的消息
        """
        return json.dumps(message, ensure_ascii=False).encode('utf-8')
    
    @staticmethod
    def deserialize_message(message_bytes):
        """
        反序列化消息
        
        Args:
            message_bytes: 序列化后的消息
            
        Returns:
            dict: 反序列化后的消息
        """
        try:
            return json.loads(message_bytes.decode('utf-8'))
        except json.JSONDecodeError:
            raise MessageFormatError("Failed to parse message as JSON")
    
    @staticmethod
    def send_message(socket, message, topic=None):
        """
        发送消息
        
        Args:
            socket: ZMQ socket
            message: 消息字典
            topic: 消息主题（如果为None，则使用消息中的主题）
        """
        # 验证消息
        CommunicationProtocol.verify_message(message)
        
        # 使用消息中的主题或传入的主题
        if topic is None:
            topic = message["header"]["topic"]
        
        # 序列化消息
        message_bytes = CommunicationProtocol.serialize_message(message)
        
        # 发送消息
        socket.send_multipart([topic.encode('utf-8'), message_bytes])
    
    @staticmethod
    def recv_message(socket):
        """
        接收消息
        
        Args:
            socket: ZMQ socket
            
        Returns:
            tuple: (topic, message)
        """
        # 接收消息
        topic_bytes, message_bytes = socket.recv_multipart()
        
        # 反序列化消息
        message = CommunicationProtocol.deserialize_message(message_bytes)
        
        # 验证消息
        CommunicationProtocol.verify_message(message)
        
        return topic_bytes.decode('utf-8'), message

# 模块发现相关常量
DISCOVERY_INTERVAL = 5  # 模块发现间隔（秒）
DISCOVERY_PORT = 5550  # 模块发现端口

class ModuleDiscovery:
    """模块发现类"""
    
    @staticmethod
    def create_discovery_message(module_info):
        """
        创建模块发现消息
        
        Args:
            module_info: 模块信息
            
        Returns:
            dict: 模块发现消息
        """
        return {
            "type": "discovery",
            "timestamp": int(time.time() * 1000),
            "module_info": module_info
        }
    
    @staticmethod
    def create_heartbeat_message(module_id):
        """
        创建心跳消息
        
        Args:
            module_id: 模块ID
            
        Returns:
            dict: 心跳消息
        """
        return {
            "type": "heartbeat",
            "timestamp": int(time.time() * 1000),
            "module_id": module_id
        }
    
    @staticmethod
    def create_module_info(
        module_id,
        module_type,
        name,
        version,
        host,
        ports,
        description=""
    ):
        """
        创建模块信息
        
        Args:
            module_id: 模块ID
            module_type: 模块类型
            name: 模块名称
            version: 模块版本
            host: 模块主机
            ports: 模块端口字典
            description: 模块描述
            
        Returns:
            dict: 模块信息
        """
        return {
            "module_id": module_id,
            "module_type": module_type,
            "name": name,
            "version": version,
            "host": host,
            "ports": ports,
            "description": description,
            "status": "active",
            "last_heartbeat": int(time.time() * 1000)
        }
