version=('fast_io','0.26')
try:
 import rtc_time as time 
except ImportError:
 import utime as time
import utimeq
import ucollections
type_gen=type((lambda:(yield))())
type_genf=type((lambda:(yield))) 
DEBUG=0
log=None
def set_debug(val):
 global DEBUG,log
 DEBUG=val
 if val:
  import logging
  log=logging.getLogger("uasyncio.core")
class CancelledError(Exception):
 pass
class TimeoutError(CancelledError):
 pass
class EventLoop:
 def __init__(self,runq_len=16,waitq_len=16,ioq_len=0,lp_len=0):
  self.runq=ucollections.deque((),runq_len,True)
  self._max_od=0
  self.lpq=utimeq.utimeq(lp_len)if lp_len else None
  self.ioq_len=ioq_len
  self.canned=set()
  if ioq_len:
   self.ioq=ucollections.deque((),ioq_len,True)
   self._call_io=self._call_now
  else:
   self._call_io=self.call_soon
  self.waitq=utimeq.utimeq(waitq_len)
  self.cur_task=None
 def time(self):
  return time.ticks_ms()
 def create_task(self,coro):
  assert not isinstance(coro,type_genf),'Coroutine arg expected.' 
  self.call_later_ms(0,coro)
 def _call_now(self,callback,*args): 
  if __debug__ and DEBUG:
   log.debug("Scheduling in ioq: %s",(callback,args))
  self.ioq.append(callback)
  if not isinstance(callback,type_gen):
   self.ioq.append(args)
 def max_overdue_ms(self,t=None):
  if t is not None:
   self._max_od=int(t)
  return self._max_od
 def call_after_ms(self,delay,callback,*args):
  self.call_at_lp_(time.ticks_add(self.time(),delay),callback,*args)
 def call_after(self,delay,callback,*args):
  self.call_at_lp_(time.ticks_add(self.time(),int(delay*1000)),callback,*args)
 def call_at_lp_(self,time,callback,*args):
  if self.lpq is not None:
   self.lpq.push(time,callback,args)
   if isinstance(callback,type_gen):
    callback.pend_throw(id(callback))
  else:
   raise OSError('No low priority queue exists.')
 def call_soon(self,callback,*args):
  if __debug__ and DEBUG:
   log.debug("Scheduling in runq: %s",(callback,args))
  self.runq.append(callback)
  if not isinstance(callback,type_gen):
   self.runq.append(args)
 def call_later(self,delay,callback,*args):
  self.call_at_(time.ticks_add(self.time(),int(delay*1000)),callback,args)
 def call_later_ms(self,delay,callback,*args):
  if not delay:
   return self.call_soon(callback,*args)
  self.call_at_(time.ticks_add(self.time(),delay),callback,args)
 def call_at_(self,time,callback,args=()):
  if __debug__ and DEBUG:
   log.debug("Scheduling in waitq: %s",(time,callback,args))
  self.waitq.push(time,callback,args)
  if isinstance(callback,type_gen):
   callback.pend_throw(id(callback))
 def wait(self,delay):
  if __debug__ and DEBUG:
   log.debug("Sleeping for: %s",delay)
  time.sleep_ms(delay)
 def run_forever(self):
  cur_task=[0,0,0]
  def runq_add():
   if isinstance(cur_task[1],type_gen):
    tid=id(cur_task[1])
    if tid in self.canned:
     self.canned.remove(tid)
    else:
     cur_task[1].pend_throw(None)
     self.call_soon(cur_task[1],*cur_task[2])
   else:
    self.call_soon(cur_task[1],*cur_task[2])
  while True:
   tnow=self.time()
   if self.lpq:
    to_run=False 
    t=self.lpq.peektime()
    tim=time.ticks_diff(t,tnow)
    to_run=self._max_od>0 and tim<-self._max_od
    if not(to_run or self.runq): 
     to_run=tim<=0 
     if to_run and self.waitq: 
      t=self.waitq.peektime()
      to_run=time.ticks_diff(t,tnow)>0 
    if to_run:
     self.lpq.pop(cur_task)
     runq_add()
   while self.waitq:
    t=self.waitq.peektime()
    delay=time.ticks_diff(t,tnow)
    if delay>0:
     break
    self.waitq.pop(cur_task)
    if __debug__ and DEBUG:
     log.debug("Moving from waitq to runq: %s",cur_task[1])
    runq_add()
   l=len(self.runq)
   if __debug__ and DEBUG:
    log.debug("Entries in runq: %d",l)
   cur_q=self.runq 
   dl=1 
   while l or self.ioq_len:
    if self.ioq_len: 
     self.wait(0) 
     if self.ioq:
      cur_q=self.ioq
      dl=0 
     elif l==0:
      break 
     else:
      cur_q=self.runq
      dl=1
    l-=dl
    cb=cur_q.popleft() 
    args=()
    if not isinstance(cb,type_gen): 
     args=cur_q.popleft()
     l-=dl
     if __debug__ and DEBUG:
      log.info("Next callback to run: %s",(cb,args))
     cb(*args) 
     continue 
    if __debug__ and DEBUG:
     log.info("Next coroutine to run: %s",(cb,args))
    self.cur_task=cb 
    delay=0
    low_priority=False 
    try:
     if args is():
      ret=next(cb) 
     else:
      ret=cb.send(*args)
     if __debug__ and DEBUG:
      log.info("Coroutine %s yield result: %s",cb,ret)
     if isinstance(ret,SysCall1): 
      arg=ret.arg
      if isinstance(ret,SleepMs):
       delay=arg
       if isinstance(ret,AfterMs):
        low_priority=True
        if isinstance(ret,After):
         delay=int(delay*1000)
      elif isinstance(ret,IORead): 
       cb.pend_throw(False) 
       self.add_reader(arg,cb) 
       continue 
      elif isinstance(ret,IOWrite): 
       cb.pend_throw(False)
       self.add_writer(arg,cb)
       continue
      elif isinstance(ret,IOReadDone): 
       self.remove_reader(arg)
       self._call_io(cb,args) 
       continue
      elif isinstance(ret,IOWriteDone):
       self.remove_writer(arg)
       self._call_io(cb,args) 
       continue 
      elif isinstance(ret,StopLoop): 
       return arg
      else:
       assert False,"Unknown syscall yielded: %r (of type %r)"%(ret,type(ret))
     elif isinstance(ret,type_gen): 
      self.call_soon(ret) 
     elif isinstance(ret,int): 
      delay=ret
     elif ret is None:
      pass
     elif ret is False:
      continue
     else:
      assert False,"Unsupported coroutine yield value: %r (of type %r)"%(ret,type(ret))
    except StopIteration as e:
     if __debug__ and DEBUG:
      log.debug("Coroutine finished: %s",cb)
     continue
    except CancelledError as e:
     if __debug__ and DEBUG:
      log.debug("Coroutine cancelled: %s",cb)
     continue
    if low_priority:
     self.call_after_ms(delay,cb) 
    elif delay:
     self.call_later_ms(delay,cb)
    else:
     self.call_soon(cb)
   delay=0
   if not self.runq:
    delay=-1
    if self.waitq:
     tnow=self.time()
     t=self.waitq.peektime()
     delay=time.ticks_diff(t,tnow)
     if delay<0:
      delay=0
    if self.lpq:
     t=self.lpq.peektime()
     lpdelay=time.ticks_diff(t,tnow)
     if lpdelay<0:
      lpdelay=0
     if lpdelay<delay or delay<0:
      delay=lpdelay 
   self.wait(delay)
 def run_until_complete(self,coro):
  assert not isinstance(coro,type_genf),'Coroutine arg expected.' 
  def _run_and_stop():
   ret=yield from coro 
   yield StopLoop(ret)
  self.call_soon(_run_and_stop())
  return self.run_forever()
 def stop(self):
  self.call_soon((lambda:(yield StopLoop(0)))())
 def close(self):
  pass
class SysCall:
 def __init__(self,*args):
  self.args=args
 def handle(self):
  raise NotImplementedError
class SysCall1(SysCall):
 def __init__(self,arg):
  self.arg=arg
class StopLoop(SysCall1):
 pass
class IORead(SysCall1):
 pass
class IOWrite(SysCall1):
 pass
class IOReadDone(SysCall1):
 pass
class IOWriteDone(SysCall1):
 pass
_event_loop=None
_event_loop_class=EventLoop
def get_event_loop(runq_len=16,waitq_len=16,ioq_len=0,lp_len=0):
 global _event_loop
 if _event_loop is None:
  _event_loop=_event_loop_class(runq_len,waitq_len,ioq_len,lp_len)
 return _event_loop
def get_running_loop():
 if _event_loop is None:
  raise RuntimeError('Event loop not instantiated')
 return _event_loop
def got_event_loop(): 
 return _event_loop is not None
def sleep(secs):
 yield int(secs*1000)
class SleepMs(SysCall1):
 def __init__(self):
  self.v=None
  self.arg=None
 def __call__(self,arg):
  self.v=arg
  return self
 def __iter__(self):
  return self
 def __next__(self):
  if self.v is not None:
   self.arg=self.v
   self.v=None
   return self
  _stop_iter.__traceback__=None
  raise _stop_iter
_stop_iter=StopIteration()
sleep_ms=SleepMs()
def cancel(coro):
 prev=coro.pend_throw(CancelledError())
 if prev is False: 
  _event_loop._call_io(coro)
 elif isinstance(prev,int): 
  _event_loop.canned.add(prev) 
  _event_loop._call_io(coro) 
 else:
  assert prev is None
class TimeoutObj:
 def __init__(self,coro):
  self.coro=coro
def wait_for_ms(coro,timeout):
 def waiter(coro,timeout_obj):
  res=yield from coro
  if __debug__ and DEBUG:
   log.debug("waiter: cancelling %s",timeout_obj)
  timeout_obj.coro=None
  return res
 def timeout_func(timeout_obj):
  if timeout_obj.coro:
   if __debug__ and DEBUG:
    log.debug("timeout_func: cancelling %s",timeout_obj.coro)
   prev=timeout_obj.coro.pend_throw(TimeoutError())
   if prev is False: 
    _event_loop._call_io(timeout_obj.coro)
   elif isinstance(prev,int): 
    _event_loop.canned.add(prev) 
    _event_loop._call_io(timeout_obj.coro) 
   else:
    assert prev is None
 timeout_obj=TimeoutObj(_event_loop.cur_task)
 _event_loop.call_later_ms(timeout,timeout_func,timeout_obj)
 return(yield from waiter(coro,timeout_obj))
def wait_for(coro,timeout):
 return wait_for_ms(coro,int(timeout*1000))
def coroutine(f):
 return f
class AfterMs(SleepMs):
 pass
class After(AfterMs):
 pass
after_ms=AfterMs()
after=After()
def ensure_future(coro,loop=_event_loop):
 _event_loop.call_soon(coro)
 return coro
def Task(coro,loop=_event_loop):
 _event_loop.call_soon(coro)
# Created by pyminifier (https://github.com/liftoff/pyminifier)
