import datetime

def get_current_time():
    """获取当前系统时间"""
    now = datetime.datetime.now()
    # 格式化为：2023年10月27日 星期五 14:30:05
    weekday_dict = {0: '星期一', 1: '星期二', 2: '星期三', 3: '星期四', 4: '星期五', 5: '星期六', 6: '星期日'}
    weekday = weekday_dict[now.weekday()]
    return now.strftime(f"%Y年%m月%d日 {weekday} %H:%M:%S")

if __name__ == "__main__":
    print(get_current_time())
