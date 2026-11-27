if __name__ == "__main__" and not __package__:
    from path_setup import ensure_project_root
    ensure_project_root(__file__, 1)

from common.clock import format_time

def get_current_time():
    """获取当前真实时间、星期、时区和 UTC 偏移，默认北京时间。"""
    # 旧实现 datetime.now() 跟随容器的 UTC，曾导致北京时间少 8 小时。
    return format_time()

if __name__ == "__main__":
    print(get_current_time())
