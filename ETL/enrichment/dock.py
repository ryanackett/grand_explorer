import socket, http.client, json, time, struct

SOCK="/var/run/docker.sock"
class UHTTP(http.client.HTTPConnection):
    def __init__(s): super().__init__("localhost")
    def connect(s):
        s.sock=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.sock.connect(SOCK)

def req(method, path, body=None):
    c=UHTTP(); c.request(method, path, body=json.dumps(body) if body is not None else None,
                         headers={"Content-Type":"application/json"})
    r=c.getresponse(); data=r.read(); c.close()
    return r.status, data

def demux(raw):
    out=[]; i=0
    while i+8<=len(raw):
        _,_,_,_,n = struct.unpack(">BBBBI", raw[i:i+8]); i+=8
        out.append(raw[i:i+n].decode("utf-8","replace")); i+=n
    return "".join(out)

CID="fluxapp_devcontainer-db-1"
def exec_run(cmd):
    st,data=req("POST", f"/containers/{CID}/exec",
                {"AttachStdout":True,"AttachStderr":True,"Cmd":cmd})
    eid=json.loads(data)["Id"]
    st,raw=req("POST", f"/exec/{eid}/start", {"Detach":False,"Tty":False})
    return demux(raw)

if __name__=="__main__":
    import sys
    if sys.argv[1]=="start":
        print(req("POST", f"/containers/{CID}/start"))
    elif sys.argv[1]=="exec":
        print(exec_run(sys.argv[2:]))
