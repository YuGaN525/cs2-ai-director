from desktop_ui import schedule
"""Small shared account panel, using the existing Tk theme."""
import threading
from tkinter import ttk


def mount_account(parent,account):
    frame=ttk.LabelFrame(parent,text='网站账号与设备',padding=(12,8))
    frame.pack(fill='x',padx=22,pady=(8,2))
    row=ttk.Frame(frame);row.pack(fill='x')
    label=ttk.Label(row,text=account.status,wraplength=700,style='Body.TLabel')
    label.pack(side='left',fill='x',expand=True)
    ttk.Button(row,text='登录网站账号',command=account.login_async,
               style='Accent.TButton').pack(side='left',padx=(10,6))
    ttk.Button(row,text='退出账号',command=lambda:threading.Thread(target=account.logout,daemon=True).start()).pack(side='left')
    note=ttk.Label(frame,text='本应用采集及你主动导入的训练数据先保存在本机；登录后自动上传当前账号的待传记录，用于导播模型训练。',
              wraplength=960,style='Caption.TLabel')
    note.pack(anchor='w',pady=(5,0))
    def wrap(event):
        width=max(260,event.width-28)
        label.configure(wraplength=max(180,width-230))
        note.configure(wraplength=width)
    frame.bind('<Configure>',wrap,add='+')
    def poll():
        if not frame.winfo_exists():return
        label.configure(text=account.status)
        schedule(frame,500,poll)
    schedule(frame,500,poll)
    return frame
