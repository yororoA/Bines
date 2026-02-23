"""
工具依赖注册表
用于在运行时注入依赖，避免循环导入

[职责分离] 工具函数通过此模块获取依赖，而不是直接访问 handle_zmq.py 中的全局变量。
这样可以避免循环导入，保持代码结构清晰。
"""
class ToolDependencies:
    """工具依赖容器"""
    def __init__(self):
        self.memory_system = None
        self.zmq_context = None
        self.thinking_model_helper = None
        self.game_mode_enabled = False
        self.game_mode_interval = 0.1
        # 播放本地音频（sing 工具）时使用的 PUB socket，由 handle_zmq 绑定并注册
        self.audio_play_pub_socket = None
        # 用于获取工具定义和注册表的回调函数（避免循环导入）
        self.get_tools_schema = None
        self.get_tools_registry = None
    
    def register_memory_system(self, memory_system):
        """注册记忆系统"""
        self.memory_system = memory_system
    
    def register_zmq_context(self, context):
        """注册 ZMQ 上下文"""
        self.zmq_context = context

    def register_audio_play_pub_socket(self, socket):
        """注册用于播放本地音频的 PUB socket（sing 工具发往 Display）"""
        self.audio_play_pub_socket = socket
    
    def register_thinking_model_helper(self, helper):
        """注册思考模型助手"""
        self.thinking_model_helper = helper
    
    def set_game_mode(self, enabled, interval=0.1):
        """设置游戏模式状态"""
        self.game_mode_enabled = enabled
        self.game_mode_interval = interval
    
    def register_tools_accessors(self, get_tools_schema, get_tools_registry):
        """
        注册工具定义和注册表的访问器（回调函数）
        
        用于 thinking_tool.py 中获取工具定义，避免循环导入
        
        Args:
            get_tools_schema: 返回 TOOLS_SCHEMA 的函数
            get_tools_registry: 返回 TOOLS_REGISTRY 的函数
        """
        self.get_tools_schema = get_tools_schema
        self.get_tools_registry = get_tools_registry

# 全局依赖实例
deps = ToolDependencies()
