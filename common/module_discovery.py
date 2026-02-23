# -*- coding: utf-8 -*-
"""
模块发现服务
负责模块的注册、发现和心跳检测
"""

import time
import threading
import json
import uuid
import zmq
from common.communication import (
    CommunicationProtocol,
    TOPICS,
    MESSAGE_TYPES,
    MODULE_TYPES,
    DISCOVERY_PORT,
    DISCOVERY_INTERVAL
)
from config import ZMQ_HOST, ZMQ_PORTS

class ModuleManager:
    """模块管理器类"""
    
    def __init__(self):
        self.modules = {}  # 模块字典，key为模块ID，value为模块信息
        self.lock = threading.Lock()  # 线程锁
        self.context = zmq.Context()  # ZMQ上下文
        self.discovery_socket = None  # 发现服务socket
        self.running = False  # 运行状态
        self.heartbeat_thread = None  # 心跳检测线程
        self.message_thread = None  # 消息处理线程
    
    def start(self):
        """启动模块发现服务"""
        if self.running:
            return
        
        self.running = True
        
        # 创建ZMQ socket
        self.discovery_socket = self.context.socket(zmq.REP)
        # 发现服务端口统一由 config 管理
        self.discovery_socket.bind(f"tcp://*:{ZMQ_PORTS['DISCOVERY']}")
        
        print(f"🚀 模块发现服务已启动，监听端口: {DISCOVERY_PORT}")
        
        # 启动心跳检测线程
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_checker)
        self.heartbeat_thread.daemon = True
        self.heartbeat_thread.start()
        
        # 启动消息处理线程
        self.message_thread = threading.Thread(target=self._message_handler)
        self.message_thread.daemon = True
        self.message_thread.start()
    
    def stop(self):
        """停止模块发现服务"""
        self.running = False
        
        # 等待消息处理线程结束
        if self.message_thread:
            self.message_thread.join(timeout=1.0)
        
        # 等待心跳检测线程结束
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=1.0)
        
        # 关闭ZMQ socket
        if self.discovery_socket:
            try:
                self.discovery_socket.close()
            except:
                pass
        
        # 终止ZMQ上下文
        if self.context:
            try:
                self.context.term()
            except:
                pass
        
        print("⏹️ 模块发现服务已停止")
    
    def _message_handler(self):
        """消息处理线程"""
        while self.running:
            message = None  # 【修复】预初始化，避免 except 中 UnboundLocalError
            try:
                # 接收消息
                message_bytes = self.discovery_socket.recv()
                message = CommunicationProtocol.deserialize_message(message_bytes)
                
                # 验证消息
                CommunicationProtocol.verify_message(message)
                
                # 处理消息
                response = self._handle_message(message)
                
                # 发送响应
                response_bytes = CommunicationProtocol.serialize_message(response)
                self.discovery_socket.send(response_bytes)
                
            except zmq.error.ContextTerminated:
                # Context已终止，退出线程
                print("📌 Context已终止，退出消息处理线程")
                break
            except Exception as e:
                # 检查socket是否仍然有效
                if not self.running:
                    break
                
                try:
                    # 发送错误响应
                    error_response = CommunicationProtocol.create_message(
                        message_type=MESSAGE_TYPES["RESPONSE"],
                        module_type=MODULE_TYPES["THINKING"],
                        content={
                            "status": "error",
                            "message": str(e)
                        },
                        request_id=message.get("header", {}).get("message_id") if isinstance(message, dict) else None
                    )
                    
                    error_bytes = CommunicationProtocol.serialize_message(error_response)
                    self.discovery_socket.send(error_bytes)
                    
                    print(f"❌ 处理消息时出错: {e}")
                except:
                    # 如果无法发送响应，可能是socket已关闭，退出线程
                    break
    
    def _handle_message(self, message):
        """处理消息"""
        message_type = message["header"]["message_type"]
        content = message["body"]
        request_id = message["header"]["message_id"]
        
        if message_type == MESSAGE_TYPES["REQUEST"]:
            action = content.get("action")
            
            if action == "register":
                return self._register_module(content["module_info"])
            elif action == "unregister":
                return self._unregister_module(content["module_id"])
            elif action == "discover":
                return self._discover_modules(content.get("module_type"))
            elif action == "list":
                return self._list_modules()
            else:
                return CommunicationProtocol.create_message(
                    message_type=MESSAGE_TYPES["RESPONSE"],
                    module_type=MODULE_TYPES["THINKING"],
                    content={
                        "status": "error",
                        "message": f"Unknown action: {action}"
                    },
                    request_id=request_id
                )
        elif message_type == MESSAGE_TYPES["NOTIFICATION"]:
            notification_type = content.get("type")
            
            if notification_type == "heartbeat":
                return self._update_heartbeat(content["module_id"])
            else:
                return CommunicationProtocol.create_message(
                    message_type=MESSAGE_TYPES["RESPONSE"],
                    module_type=MODULE_TYPES["THINKING"],
                    content={
                        "status": "error",
                        "message": f"Unknown notification type: {notification_type}"
                    },
                    request_id=request_id
                )
        else:
            return CommunicationProtocol.create_message(
                message_type=MESSAGE_TYPES["RESPONSE"],
                module_type=MODULE_TYPES["THINKING"],
                content={
                    "status": "error",
                    "message": f"Unknown message type: {message_type}"
                },
                request_id=request_id
            )
    
    def _register_module(self, module_info):
        """注册模块"""
        module_id = module_info.get("module_id")
        
        if not module_id:
            # 生成新的模块ID
            module_id = str(uuid.uuid4())
            module_info["module_id"] = module_id
        
        # 更新模块信息
        module_info["last_heartbeat"] = int(time.time() * 1000)
        
        with self.lock:
            self.modules[module_id] = module_info
        
        print(f"📝 模块已注册: {module_info.get('name', 'Unknown')} (ID: {module_id})")
        
        return CommunicationProtocol.create_message(
            message_type=MESSAGE_TYPES["RESPONSE"],
            module_type=MODULE_TYPES["THINKING"],
            content={
                "status": "success",
                "module_id": module_id,
                "message": "Module registered successfully"
            }
        )
    
    def _unregister_module(self, module_id):
        """注销模块"""
        with self.lock:
            if module_id in self.modules:
                module_name = self.modules[module_id].get('name', 'Unknown')
                del self.modules[module_id]
                print(f"❌ 模块已注销: {module_name} (ID: {module_id})")
                
                return CommunicationProtocol.create_message(
                    message_type=MESSAGE_TYPES["RESPONSE"],
                    module_type=MODULE_TYPES["THINKING"],
                    content={
                        "status": "success",
                        "message": "Module unregistered successfully"
                    }
                )
            else:
                return CommunicationProtocol.create_message(
                    message_type=MESSAGE_TYPES["RESPONSE"],
                    module_type=MODULE_TYPES["THINKING"],
                    content={
                        "status": "error",
                        "message": f"Module not found: {module_id}"
                    }
                )
    
    def _discover_modules(self, module_type=None):
        """发现模块"""
        with self.lock:
            if module_type:
                # 按模块类型过滤
                discovered_modules = [
                    module for module in self.modules.values()
                    if module["module_type"] == module_type
                ]
            else:
                # 返回所有模块
                discovered_modules = list(self.modules.values())
        
        return CommunicationProtocol.create_message(
            message_type=MESSAGE_TYPES["RESPONSE"],
            module_type=MODULE_TYPES["THINKING"],
            content={
                "status": "success",
                "modules": discovered_modules
            }
        )
    
    def _list_modules(self):
        """列出所有模块"""
        with self.lock:
            module_list = list(self.modules.values())
        
        return CommunicationProtocol.create_message(
            message_type=MESSAGE_TYPES["RESPONSE"],
            module_type=MODULE_TYPES["THINKING"],
            content={
                "status": "success",
                "modules": module_list,
                "count": len(module_list)
            }
        )
    
    def _update_heartbeat(self, module_id):
        """更新模块心跳"""
        with self.lock:
            if module_id in self.modules:
                self.modules[module_id]["last_heartbeat"] = int(time.time() * 1000)
                self.modules[module_id]["status"] = "active"
                
                return CommunicationProtocol.create_message(
                    message_type=MESSAGE_TYPES["RESPONSE"],
                    module_type=MODULE_TYPES["THINKING"],
                    content={
                        "status": "success",
                        "message": "Heartbeat updated"
                    }
                )
            else:
                return CommunicationProtocol.create_message(
                    message_type=MESSAGE_TYPES["RESPONSE"],
                    module_type=MODULE_TYPES["THINKING"],
                    content={
                        "status": "error",
                        "message": f"Module not found: {module_id}"
                    }
                )
    
    def _heartbeat_checker(self):
        """心跳检测线程"""
        while self.running:
            time.sleep(DISCOVERY_INTERVAL)
            
            current_time = int(time.time() * 1000)
            timeout = DISCOVERY_INTERVAL * 2 * 1000  # 超时时间为发现间隔的2倍
            
            with self.lock:
                # 检查所有模块的心跳
                for module_id in list(self.modules.keys()):
                    last_heartbeat = self.modules[module_id]["last_heartbeat"]
                    
                    if current_time - last_heartbeat > timeout:
                        # 模块超时，标记为离线
                        print(f"⚠️ 模块超时: {self.modules[module_id].get('name', 'Unknown')} (ID: {module_id})")
                        self.modules[module_id]["status"] = "inactive"
                        
                        # 超过3个周期未收到心跳，移除模块
                        if current_time - last_heartbeat > timeout * 3:
                            print(f"❌ 模块已移除: {self.modules[module_id].get('name', 'Unknown')} (ID: {module_id})")
                            del self.modules[module_id]

class ModuleClient:
    """模块客户端类"""
    
    def __init__(self, module_info):
        self.module_info = module_info
        self.module_id = module_info.get("module_id", str(uuid.uuid4()))
        self.module_info["module_id"] = self.module_id
        
        self.context = zmq.Context()
        self.socket = None
        self.running = False
        self.heartbeat_thread = None
        
        # 注册模块
        self.registered = False
    
    def connect(self, discovery_server=None):
        """连接到模块发现服务器"""
        try:
            if discovery_server is None:
                discovery_server = f"tcp://{ZMQ_HOST}:{ZMQ_PORTS['DISCOVERY']}"
            # 创建ZMQ socket
            self.socket = self.context.socket(zmq.REQ)
            self.socket.connect(discovery_server)
            
            # 注册模块
            self._register()
            
            # 启动心跳线程
            self.running = True
            self.heartbeat_thread = threading.Thread(target=self._heartbeat_sender)
            self.heartbeat_thread.daemon = True
            self.heartbeat_thread.start()
            
            return True
        except Exception as e:
            print(f"❌ 连接到模块发现服务器失败: {e}")
            return False
    
    def disconnect(self):
        """断开连接"""
        self.running = False
        
        # 注销模块
        self._unregister()
        
        # 等待心跳线程结束
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=1.0)
        
        # 关闭socket
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        
        # 终止ZMQ上下文
        if self.context:
            try:
                self.context.term()
            except:
                pass
    
    def _register(self):
        """注册模块"""
        try:
            # 创建注册请求
            register_request = CommunicationProtocol.create_message(
                message_type=MESSAGE_TYPES["REQUEST"],
                module_type=self.module_info["module_type"],
                content={
                    "action": "register",
                    "module_info": self.module_info
                }
            )
            
            # 发送请求
            message_bytes = CommunicationProtocol.serialize_message(register_request)
            self.socket.send(message_bytes)
            
            # 接收响应
            response_bytes = self.socket.recv()
            response = CommunicationProtocol.deserialize_message(response_bytes)
            
            # 验证响应
            CommunicationProtocol.verify_message(response)
            
            # 处理响应
            if response["body"]["status"] == "success":
                self.registered = True
                self.module_id = response["body"]["module_id"]
                self.module_info["module_id"] = self.module_id
                print(f"✅ 模块注册成功，ID: {self.module_id}")
            else:
                print(f"❌ 模块注册失败: {response['body']['message']}")
        except Exception as e:
            print(f"❌ 注册模块时出错: {e}")
    
    def _unregister(self):
        """注销模块"""
        if not self.registered:
            return
        
        try:
            # 创建注销请求
            unregister_request = CommunicationProtocol.create_message(
                message_type=MESSAGE_TYPES["REQUEST"],
                module_type=self.module_info["module_type"],
                content={
                    "action": "unregister",
                    "module_id": self.module_id
                }
            )
            
            # 发送请求
            message_bytes = CommunicationProtocol.serialize_message(unregister_request)
            self.socket.send(message_bytes)
            
            # 接收响应
            response_bytes = self.socket.recv()
            response = CommunicationProtocol.deserialize_message(response_bytes)
            
            # 验证响应
            CommunicationProtocol.verify_message(response)
            
            if response["body"]["status"] == "success":
                self.registered = False
                print(f"✅ 模块注销成功")
            else:
                print(f"❌ 模块注销失败: {response['body']['message']}")
        except Exception as e:
            print(f"❌ 注销模块时出错: {e}")
    
    def _heartbeat_sender(self):
        """心跳发送线程"""
        while self.running:
            try:
                # 创建心跳请求
                heartbeat_request = CommunicationProtocol.create_message(
                    message_type=MESSAGE_TYPES["NOTIFICATION"],
                    module_type=self.module_info["module_type"],
                    content={
                        "type": "heartbeat",
                        "module_id": self.module_id
                    }
                )
                
                # 发送请求
                message_bytes = CommunicationProtocol.serialize_message(heartbeat_request)
                self.socket.send(message_bytes)
                
                # 接收响应
                response_bytes = self.socket.recv()
                response = CommunicationProtocol.deserialize_message(response_bytes)
                
                # 验证响应
                CommunicationProtocol.verify_message(response)
                
            except Exception as e:
                print(f"❌ 发送心跳时出错: {e}")
            
            # 等待指定间隔
            time.sleep(DISCOVERY_INTERVAL)
    
    def discover_modules(self, module_type=None):
        """发现模块"""
        try:
            # 创建发现请求
            discover_request = CommunicationProtocol.create_message(
                message_type=MESSAGE_TYPES["REQUEST"],
                module_type=self.module_info["module_type"],
                content={
                    "action": "discover",
                    "module_type": module_type
                }
            )
            
            # 发送请求
            message_bytes = CommunicationProtocol.serialize_message(discover_request)
            self.socket.send(message_bytes)
            
            # 接收响应
            response_bytes = self.socket.recv()
            response = CommunicationProtocol.deserialize_message(response_bytes)
            
            # 验证响应
            CommunicationProtocol.verify_message(response)
            
            if response["body"]["status"] == "success":
                return response["body"]["modules"]
            else:
                print(f"❌ 发现模块失败: {response['body']['message']}")
                return []
        except Exception as e:
            print(f"❌ 发现模块时出错: {e}")
            return []
    
    def list_modules(self):
        """列出所有模块"""
        try:
            # 创建列表请求
            list_request = CommunicationProtocol.create_message(
                message_type=MESSAGE_TYPES["REQUEST"],
                module_type=self.module_info["module_type"],
                content={
                    "action": "list"
                }
            )
            
            # 发送请求
            message_bytes = CommunicationProtocol.serialize_message(list_request)
            self.socket.send(message_bytes)
            
            # 接收响应
            response_bytes = self.socket.recv()
            response = CommunicationProtocol.deserialize_message(response_bytes)
            
            # 验证响应
            CommunicationProtocol.verify_message(response)
            
            if response["body"]["status"] == "success":
                return response["body"]["modules"]
            else:
                print(f"❌ 列出模块失败: {response['body']['message']}")
                return []
        except Exception as e:
            print(f"❌ 列出模块时出错: {e}")
            return []

if __name__ == "__main__":
    # 测试模块发现服务
    module_manager = ModuleManager()
    
    try:
        module_manager.start()
        
        # 保持运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        module_manager.stop()