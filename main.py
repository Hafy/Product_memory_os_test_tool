import sys
import os
import tkinter as tk
from gui.app import RMTTestApp
from utils.constants import get_resource_path

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            import ctypes
            myappid = 'mycompany.myproduct.subproduct.version'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        root = tk.Tk()
        root.title("内存自动化测试工具")
        root.iconbitmap(default=get_resource_path('tigo.ico'))
        app = RMTTestApp(root)
        root.mainloop()
    except Exception as e:
        import logging
        logging.exception("程序发生未捕获异常")
        input("发生错误，按回车退出")
