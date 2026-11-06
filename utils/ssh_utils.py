import subprocess
import paramiko
import socket
from .constants import SSH_TIMEOUT

def ssh_run(cmd, ip, user, password=None, key_path=None, silent=False, timeout=SSH_TIMEOUT):
    """
    执行 SSH 命令，支持密钥或密码认证（纯 paramiko 实现）
    优先级：key_path > password
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, 22))
        
        if key_path:
            # 使用密钥认证
            private_key = paramiko.RSAKey.from_private_key_file(key_path)
            ssh.connect(hostname=ip, username=user, pkey=private_key,
                        timeout=SSH_TIMEOUT, sock=sock, look_for_keys=False, allow_agent=False)
        elif password:
            # 使用密码认证
            ssh.connect(hostname=ip, username=user, password=password,
                        timeout=SSH_TIMEOUT, sock=sock, look_for_keys=False, allow_agent=False)
        else:
            raise Exception("需要提供密码或密钥")
        
        _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        ret = stdout.channel.recv_exit_status()
        
        if ret != 0 and not silent:
            raise RuntimeError(f"SSH执行失败(exit {ret}): {err}")
        return out
        
    except socket.timeout:
        raise TimeoutError(f"SSH连接超时{timeout}秒")
    except paramiko.AuthenticationException:
        if password:
            raise Exception("SSH 认证失败，请检查用户名和密码")
        else:
            raise Exception("SSH 认证失败，请检查密钥文件")
    except Exception as e:
        raise Exception(f"SSH执行错误:{str(e)}")
    finally:
        ssh.close()


def scp_upload(local_file, remote_path, ip, user, password=None, key_path=None):
    """上传文件，支持密钥或密码认证"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        if key_path:
            private_key = paramiko.RSAKey.from_private_key_file(key_path)
            ssh.connect(hostname=ip, username=user, pkey=private_key, timeout=SSH_TIMEOUT)
        elif password:
            ssh.connect(hostname=ip, username=user, password=password, timeout=SSH_TIMEOUT)
        else:
            raise Exception("需要提供密码或密钥")
        
        sftp = ssh.open_sftp()
        sftp.put(local_file, remote_path)
        sftp.close()
        ssh.close()
        return True
    except Exception as e:
        raise Exception(f"SCP上传失败: {e}")


def scp_down(remote_file, local_path, ip, user, password=None, key_path=None):
    """下载文件，支持密钥或密码认证"""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        if key_path:
            private_key = paramiko.RSAKey.from_private_key_file(key_path)
            ssh.connect(hostname=ip, username=user, pkey=private_key, timeout=SSH_TIMEOUT)
        elif password:
            ssh.connect(hostname=ip, username=user, password=password, timeout=SSH_TIMEOUT)
        else:
            raise Exception("需要提供密码或密钥")
        
        sftp = ssh.open_sftp()
        sftp.get(remote_file, local_path)
        sftp.close()
        ssh.close()
        return True
    except Exception as e:
        raise Exception(f"SCP下载失败: {e}")
