"""Small shared account panel, using the existing Tk theme."""
import threading
from tkinter import ttk


def mount_account(parent,account):
    frame=ttk.LabelFrame(parent,text=' 网站账号与设备 ',padding=8)
    frame.pack(fill='x',padx=10,pady=(4,6))
    row=ttk.Frame(frame);row.pack(fill='x')
    label=ttk.Label(row,text=account.status,wraplength=720);label.pack(side='left',fill='x',expand=True)
    ttk.Button(row,text='登录网站账号',command=account.login_async).pack(side='left',padx=4)
    ttk.Button(row,text='退出账号',command=lambda:threading.Thread(target=account.logout,daemon=True).start()).pack(side='left')
    ttk.Label(frame,text='本应用采集及你主动导入的训练数据先保存在本机；登录后自动上传当前账号的待传记录，用于导播模型训练。',wraplength=880).pack(anchor='w',pady=(6,0))
    def poll():
        if not frame.winfo_exists():return
        label.configure(text=account.status)
        frame.after(500,poll)
    frame.after(500,poll)
    return frame
