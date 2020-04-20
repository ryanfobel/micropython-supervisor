import uerrno
import uselect as select
import usocket as _socket
import sys
from uasyncio.core import*
DEBUG=0
log=None
def set_debug(val):
 global DEBUG,log
 DEBUG=val
 if val:
  import logging
  log=logging.getLogger("uasyncio")
class PollEventLoop(EventLoop):
 def __init__(self,runq_len=16,waitq_len=16,fast_io=0,lp_len=0):
  EventLoop.__init__(self,runq_len,waitq_len,fast_io,lp_len)
  self.poller=select.poll()
  self.rdobjmap={}
  self.wrobjmap={}
  self.flags={}
 def _unregister(self,sock,objmap,flag):
  if id(sock)in self.flags:
   flags=self.flags[id(sock)]
   if flags&flag: 
    flags&=~flag 
    if flags: 
     self.flags[id(sock)]=flags
     self.poller.register(sock,flags)
    else:
     del self.flags[id(sock)] 
     self.poller.unregister(sock)
    del objmap[id(sock)] 
 def _register(self,sock,flag):
  if id(sock)in self.flags:
   self.flags[id(sock)]|=flag
  else:
   self.flags[id(sock)]=flag
  self.poller.register(sock,self.flags[id(sock)])
 def add_reader(self,sock,cb,*args):
  if DEBUG and __debug__:
   log.debug("add_reader%s",(sock,cb,args))
  self._register(sock,select.POLLIN|select.POLLHUP|select.POLLERR) 
  if args:
   self.rdobjmap[id(sock)]=(cb,args)
  else:
   self.rdobjmap[id(sock)]=cb
 def remove_reader(self,sock):
  if DEBUG and __debug__:
   log.debug("remove_reader(%s)",sock)
  self._unregister(sock,self.rdobjmap,select.POLLIN)
 def add_writer(self,sock,cb,*args):
  if DEBUG and __debug__:
   log.debug("add_writer%s",(sock,cb,args))
  self._register(sock,select.POLLOUT|select.POLLHUP|select.POLLERR) 
  if args:
   self.wrobjmap[id(sock)]=(cb,args)
  else:
   self.wrobjmap[id(sock)]=cb
 def remove_writer(self,sock):
  if DEBUG and __debug__:
   log.debug("remove_writer(%s)",sock)
  self._unregister(sock,self.wrobjmap,select.POLLOUT)
 def wait(self,delay):
  if DEBUG and __debug__:
   log.debug("poll.wait(%d)",delay)
  res=self.poller.ipoll(delay,1)
  for sock,ev in res:
   if ev&select.POLLOUT:
    cb=self.wrobjmap[id(sock)]
    if cb is None:
     continue 
    self.wrobjmap[id(sock)]=None
    if ev&(select.POLLHUP|select.POLLERR):
     self.remove_writer(sock)
    if DEBUG and __debug__:
     log.debug("Calling IO callback: %r",cb)
    if isinstance(cb,tuple):
     cb[0](*cb[1])
    else:
     prev=cb.pend_throw(None) 
     self._call_io(cb) 
   if ev&select.POLLIN:
    cb=self.rdobjmap[id(sock)]
    if cb is None:
     continue
    self.rdobjmap[id(sock)]=None
    if ev&(select.POLLHUP|select.POLLERR):
     self.remove_reader(sock)
    if DEBUG and __debug__:
     log.debug("Calling IO callback: %r",cb)
    if isinstance(cb,tuple):
     cb[0](*cb[1])
    else:
     prev=cb.pend_throw(None) 
     self._call_io(cb)
class StreamReader:
 def __init__(self,polls,ios=None):
  if ios is None:
   ios=polls
  self.polls=polls
  self.ios=ios
 def read(self,n=-1):
  while True:
   yield IORead(self.polls)
   res=self.ios.read(n) 
   if res is not None:
    break
  yield IOReadDone(self.polls) 
  return res 
 def readinto(self,buf,n=0): 
  while True:
   yield IORead(self.polls)
   if n:
    res=self.ios.readinto(buf,n) 
   else:
    res=self.ios.readinto(buf)
   if res is not None:
    break
  yield IOReadDone(self.polls)
  return res
 def readexactly(self,n):
  buf=b""
  while n:
   yield IORead(self.polls)
   res=self.ios.read(n)
   if res is None:
    if DEBUG and __debug__:
     log.debug('WARNING: socket write returned type(None)')
    continue
   else:
    if not res: 
     break
    buf+=res
    n-=len(res)
  yield IOReadDone(self.polls)
  return buf
 def readline(self):
  if DEBUG and __debug__:
   log.debug("StreamReader.readline()")
  buf=b""
  while True:
   yield IORead(self.polls)
   res=self.ios.readline()
   if res is None:
    if DEBUG and __debug__:
     log.debug('WARNING: socket read returned type(None)')
    continue
   else:
    if not res:
     break
    buf+=res
    if buf[-1]==0x0a:
     break
  if DEBUG and __debug__:
   log.debug("StreamReader.readline(): %s",buf)
  yield IOReadDone(self.polls)
  return buf
 def aclose(self):
  yield IOReadDone(self.polls)
  self.ios.close()
 def __repr__(self):
  return "<StreamReader %r %r>"%(self.polls,self.ios)
class StreamWriter:
 def __init__(self,s,extra):
  self.s=s
  self.extra=extra
 def awrite(self,buf,off=0,sz=-1):
  if sz==-1:
   sz=len(buf)-off
  if DEBUG and __debug__:
   log.debug("StreamWriter.awrite(): spooling %d bytes",sz)
  while True:
   yield IOWrite(self.s)
   res=self.s.write(buf,off,sz)
   if res is None:
    if DEBUG and __debug__:
     log.debug('WARNING: socket write returned type(None)')
    continue
   if res==sz:
    if DEBUG and __debug__:
     log.debug("StreamWriter.awrite(): completed spooling %d bytes",res)
    break
   if DEBUG and __debug__:
    log.debug("StreamWriter.awrite(): spooled partial %d bytes",res)
   assert res<sz
   off+=res
   sz-=res
   yield IOWrite(self.s)
   if DEBUG and __debug__:
    log.debug("StreamWriter.awrite(): can write more")
  yield IOWriteDone(self.s)
 def awriteiter(self,iterable):
  for buf in iterable:
   yield from self.awrite(buf)
 def aclose(self):
  yield IOWriteDone(self.s)
  self.s.close()
 def get_extra_info(self,name,default=None):
  return self.extra.get(name,default)
 def __repr__(self):
  return "<StreamWriter %r>"%self.s
def open_connection(host,port,ssl=False):
 if DEBUG and __debug__:
  log.debug("open_connection(%s, %s)",host,port)
 ai=_socket.getaddrinfo(host,port,0,_socket.SOCK_STREAM)
 ai=ai[0]
 s=_socket.socket(ai[0],ai[1],ai[2])
 s.setblocking(False)
 try:
  s.connect(ai[-1])
 except OSError as e:
  if e.args[0]!=uerrno.EINPROGRESS:
   raise
 if DEBUG and __debug__:
  log.debug("open_connection: After connect")
 yield IOWrite(s)
 if DEBUG and __debug__:
  log.debug("open_connection: After iowait: %s",s)
 if ssl:
  print("Warning: uasyncio SSL support is alpha")
  import ussl
  s.setblocking(True)
  s2=ussl.wrap_socket(s)
  s.setblocking(False)
  return StreamReader(s,s2),StreamWriter(s2,{})
 return StreamReader(s),StreamWriter(s,{})
def start_server(client_coro,host,port,backlog=10):
 if DEBUG and __debug__:
  log.debug("start_server(%s, %s)",host,port)
 ai=_socket.getaddrinfo(host,port,0,_socket.SOCK_STREAM)
 ai=ai[0]
 s=_socket.socket(ai[0],ai[1],ai[2])
 s.setblocking(False)
 s.setsockopt(_socket.SOL_SOCKET,_socket.SO_REUSEADDR,1)
 s.bind(ai[-1])
 s.listen(backlog)
 try:
  while True:
   try:
    if DEBUG and __debug__:
     log.debug("start_server: Before accept")
    yield IORead(s)
    if DEBUG and __debug__:
     log.debug("start_server: After iowait")
    s2,client_addr=s.accept()
    s2.setblocking(False)
    if DEBUG and __debug__:
     log.debug("start_server: After accept: %s",s2)
    extra={"peername":client_addr}
    yield client_coro(StreamReader(s2),StreamWriter(s2,extra))
    s2=None
   except Exception as e:
    if len(e.args)==0:
     print('start_server:Unknown error: continuing')
     sys.print_exception(e)
    if not uerrno.errorcode.get(e.args[0],False):
     print('start_server:Unexpected error: terminating')
     raise
 finally:
  if s2:
   s2.close()
  s.close()
import uasyncio.core
uasyncio.core._event_loop_class=PollEventLoop
# Created by pyminifier (https://github.com/liftoff/pyminifier)
