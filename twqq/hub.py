#!/usr/bin/env python
# -*- coding:utf-8 -*-
#
#   Author  :   cold
#   E-mail  :   wh_linux@126.com
#   Date    :   13/11/14 13:14:54
#   Desc    :   请求中间件
#
import re
import os
import time
import copy
import json
import random
import urllib2
import logging
import tempfile
import threading

import _hash

from hashlib import md5

from tornado.stack_context import ExceptionStackContext
from tornadohttpclient import UploadForm as Form
from tornadohttpclient import TornadoHTTPClient

from .requests import check_request, AcceptVerifyRequest
from .requests import WebQQRequest, PollMessageRequest, HeartbeatRequest
from .requests import SessMsgRequest, BuddyMsgRequest, GroupMsgRequest
from .requests import FirstRequest, Login2Request, DiscuMsgRequest

import const
logger = logging.getLogger("twqq")


class RequestHub(object):

    """ 集成Request请求和保存请求值
    :param qid: qq号
    :param pwd: 密码
    :param client: ~twqq.client.Client instance
    """
    SIG_RE = re.compile(r'var g_login_sig=encodeURIComponent\("(.*?)"\);')

    def __init__(self, qid, pwd, client=None, debug=False):
        self.http = TornadoHTTPClient()
        self.http.set_user_agent(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Ubuntu Chromium/28.0.1500.71 "
            "Chrome/28.0.1500.71 Safari/537.36")
        self.http.validate_cert = False
        self.http.set_global_headers({"Accept-Charset": "UTF-8,*;q=0.5"})
        self.http.debug = debug

        self.qid = qid
        self.__pwd = pwd
        self.client = client

        self.rc = random.randrange(0, 100)
        self.aid = 1003903                                    # aid 固定
        self.clientid = random.randrange(11111111, 99999999)  # 客户端id 随机固定
        self.msg_id = random.randrange(11111111, 99999999)     # 消息id, 随机初始化
        self.daid = 164
        self.login_sig = None
        self.ptwebqq = None
        self.nickname = u"YouWillNeverGetIt"
        self.vfwebqq = None
        self.psessionid = None
        self.stop_poll = False

        # 检查是否验证码的回调
        self.ptui_checkVC = lambda r, v, u: (r, v, u)

        # 是否需要验证码
        self.require_check = None
        self.require_check_time = None

        # 是否开始心跳和拉取消息
        self.poll_and_heart = None
        self.login_time = None
        self.hThread = None

        # 验证图片
        self.checkimg_path = tempfile.mktemp(".jpg")
        self._lock_path = tempfile.mktemp()
        self._wait_path = tempfile.mktemp()

        self.friend_info = {}        # 初始化好友列表
        self.group_info = {}         # 初始化组列表
        self.group_sig = {}          # 组签名映射, 用作发送临时消息(sess_message)
        self.group_members_info = {}  # 初始化组成员列表
        self.mark_to_uin = {}        # 备注名->uin的映射

        self.message_interval = 0.5  # 消息间隔
        self.last_msg_time = time.time()
        self.last_msg_content = None
        self.last_msg_numbers = 0    # 剩余位发送的消息数量
        WebQQRequest.hub = self
        self.load_next_request(FirstRequest())

    def load_next_request(self, request):
        """ 加载下一个请求

        :param request: ~twqq.requests.WebQQRequest instance
        :rtype: ~twqq.requests.WebQQRequest instance
        """
        func = self.http.get if request.method == WebQQRequest.METHOD_GET \
            else self.http.post

        if self.stop_poll and isinstance(request, PollMessageRequest):
            logger.info("检测Poll已停止, 此请求不处理: {0}".format(request))
            return

        kwargs = copy.deepcopy(request.kwargs)
        callback = request.callback if hasattr(request, "callback") and\
            callable(request.callback) else None
        kwargs.update(callback=self.wrap(request, callback))
        kwargs.update(headers=request.headers)
        kwargs.update(delay=request.delay)
        logger.debug("KWARGS: {0}".format(kwargs))

        if request.ready:
            logger.debug("处理请求: {0}".format(request))
            with ExceptionStackContext(request.handle_exc):
                func(request.url, request.params, **kwargs)
        else:
            logger.debug("请求未就绪: {0}".format(request))

        return request

    def handle_pwd(self, r, vcode, huin):
        """ 根据检查返回结果,调用回调生成密码和保存验证码 """
        pwd = md5(md5(self.__pwd).digest() + huin).hexdigest().upper()
        pwd = md5(pwd + vcode).hexdigest().upper()
        return pwd

    def upload_file(self, filename, path):
        """ 上传文件

        :param filename: 文件名
        :param path: 文件路径
        """
        form = Form()
        filename = filename.encode("utf-8")
        form.add_file(fieldname='name', filename=filename,
                      fileHandle=open(path))
        img_host = "http://dimg.vim-cn.com/"
        req = urllib2.Request(img_host)
        req.add_header("Content-Type", form.get_content_type())
        req.add_header("Content-Length", len(str(form)))
        req.add_header("User-Agent", "curl/python")
        req.add_data(str(form))
        return urllib2.urlopen(req)

    def lock(self):
        """ 当输入验证码时锁住
        """
        with open(self._lock_path, 'w'):
            pass

    def unlock(self):
        """ 解锁
        """
        if os.path.exists(self._lock_path):
            os.remove(self._lock_path)

    def clean(self):
        """ 清除锁住和等待状态
        """
        self.unlock()
        self.unwait()

    def wait(self):
        """ 当没有验证是否需要验证码时等待
        """
        with open(self._wait_path, 'w'):
            pass

    def unwait(self):
        """ 解除等待状态
        """
        if os.path.exists(self._wait_path):
            os.remove(self._wait_path)

    def is_lock(self):
        """ 检测是否被锁住
        """
        return os.path.exists(self._lock_path)

    def is_wait(self):
        """ 检测是否在等待生成验证码
        """
        return os.path.exists(self._wait_path)

    def _hash(self):
        """  获取好友列表时的Hash """
        return _hash.webqq_hash(self.qid, self.ptwebqq)

    def start_poll(self):
        """ 开始心跳和拉取信息
        """
        self.stop_poll = False
        if not self.poll_and_heart:
            self.login_time = time.time()
            logger.info("开始拉取信息")
            self.load_next_request(PollMessageRequest())
            self.poll_and_heart = True
            if self.hThread is None:
                logger.info("开始心跳")
                self.hThread = threading.Thread(target=self._heartbeat)
                self.hThread.setDaemon(True)
                self.hThread.start()

    def _heartbeat(self):
        """ 放入线程的产生心跳
        """
        assert not isinstance(threading.currentThread(), threading._MainThread)
        while 1:
            try:
                self.load_next_request(HeartbeatRequest())
            except:
                pass
            time.sleep(60)

    def make_msg_content(self, content, style):
        """ 构造QQ消息的内容

        :param content: 小心内容
        :type content: str
        :rtype: str
        """
        self.msg_id += 1
        return json.dumps([content,
                           ["font", style]])

    def get_delay(self, content):
        """ 根据消息内容是否和上一条内容相同和未送出的消息数目产生延迟

        :param content: 消息内容
        :rtype: tuple(delay, number)
        """
        MIN = self.message_interval
        delay = 0
        sub = time.time() - self.last_msg_time
        if self.last_msg_numbers < 0:
            self.last_msg_numbers = 0

        # 不足最小间隔就补足最小间隔
        if sub < MIN:
            delay = MIN
            logger.debug(u"间隔 %s 小于 %s, 设置延迟为%s", sub, MIN, delay)

        # 如果间隔是已有消息间隔的2倍, 则清除已有消息数
        # print "sub", sub, "n:", self.last_msg_numbers
        if self.last_msg_numbers > 0 and\
                sub / (MIN * self.last_msg_numbers) > 1:
            self.last_msg_numbers = 0

        # 如果还有消息未发送, 则加上他们的间隔
        if self.last_msg_numbers > 0:
            delay += MIN * self.last_msg_numbers
            logger.info(u"有%s条消息未发送, 延迟为 %s",
                        self.last_msg_numbers, delay)

        n = 1
        # 如果这条消息和上条消息一致, 保险起见再加上一个最小间隔
        if self.last_msg_content == content and sub < MIN:
            delay += MIN
            self.last_msg_numbers += 1
            n = 2

        self.last_msg_numbers += 1
        self.last_msg_content = content

        if delay:
            logger.info(u"有 {1} 个消息未投递将会在 {0} 秒后投递"
                        .format(delay, self.last_msg_numbers))
        # 返回消息累加个数, 在消息发送后减去相应的数目
        return delay, n

    def consume_delay(self, number):
        """ 消费延迟

        :param number: 消费的消息数目
        """
        self.last_msg_numbers -= number
        self.last_msg_time = time.time()

    def get_group_id(self, uin):
        """ 根据组uin获取组的id

        :param uin: 组的uin
        """
        return self.group_info.get(uin, {}).get("gid")

    def get_friend_name(self, uin):
        """ 获取好友名称

        :param uin: 好友uin
        """
        info = self.friend_info.get(uin, {})
        name = info.get("markname")
        if name is None:
            name = info.get("nick")
        return name

    def wrap(self, request, func=None):
        """ 装饰callback

        :param request: ~twqq.requests.WebQQRequest instance
        :param func: 回调函数
        """
        def _wrap(resp, *args, **kwargs):
            data = resp.body
            logger.debug(resp.headers)
            if resp.headers.get("Content-Type") == "application/json":
                data = json.loads(data) if data else {}
            else:
                try:
                    data = json.loads(data)
                except:
                    pass
            if func:
                func(resp, data, *args, **kwargs)

            funcs = self.client.request_handlers.get(
                check_request(request), [])
            for f in funcs:
                f(request, resp, data)

        return _wrap

    def handle_qq_msg_contents(self, contents):
        """ 处理QQ消息内容

        :param contents: 内容
        :type contents: list
        """
        content = ""
        for row in contents:
            if isinstance(row, (str, unicode)):
                content += row.replace(u"【提示：此用户正在使用Q+"
                                       u" Web：http://web.qq.com/】", "")\
                    .replace(u"【提示：此用户正在使用Q+"
                             u" Web：http://web3.qq.com/】", "")
        return content.replace("\r", "\n").replace("\r\n", "\n")\
            .replace("\n\n", "\n")

    def get_group_member_nick(self, gcode, uin):
        """ 根据组代码和用户uin获取群成员昵称

        :param gcode: 组代码
        :param uin: 群成员uin
        """
        return self.group_members_info.get(gcode, {}).get(uin, {}).get("nick")

    def dispatch(self, qq_source):
        """ 调度QQ消息

        :param qq_source: 源消息包
        """
        if self.stop_poll:
            logger.info("检测Poll已停止, 此消息不处理: {0}".format(qq_source))
            return

        if qq_source.get("retcode") == 0:
            messages = qq_source.get("result")
            logger.info(u"获取消息: {0}".format(messages))
            for m in messages:
                funcs = self.client.msg_handlers.get(m.get("poll_type"), [])
                [func(*func._args_func(self, m)) for func in funcs]

    def relogin(self):
        """ 被T出或获取登出时尝试重新登录
        """
        self.stop_poll = True
        self.poll_and_heart = None
        self.load_next_request(Login2Request(relogin=True))

    def send_sess_msg(self, qid, to_uin, content, style=const.DEFAULT_STYLE):
        """ 发送临时消息

        :param qid: 发送临时消息的qid
        :param to_uin: 消息接收人
        :param content: 消息内容
        :rtype: Request instance
        """
        return self.load_next_request(SessMsgRequest(qid, to_uin, content,
                                                     style))

    def send_group_msg(self, group_uin, content, style=const.DEFAULT_STYLE):
        """ 发送群消息

        :param group_uin: 组的uin
        :param content: 消息内容
        :rtype: Request instance
        """
        return self.load_next_request(GroupMsgRequest(group_uin, content,
                                                      style))

    def send_discu_msg(self, did, content, style=const.DEFAULT_STYLE):
        """ 发送讨论组消息

        :param did: 讨论组id
        :param content: 内容
        """
        return self.load_next_request(DiscuMsgRequest(did, content, style))

    def send_buddy_msg(self, to_uin, content, style=const.DEFAULT_STYLE):
        """ 发送好友消息

        :param to_uin: 消息接收人
        :param content: 消息内容
        :rtype: Request instance
        """
        return self.load_next_request(BuddyMsgRequest(to_uin, content, style))

    def send_msg_with_markname(self, markname, content):
        """ 使用备注名发送消息

        :param markname: 备注名
        :param content: 消息内容
        :rtype: None or Request instance
        """
        uin = self.mark_to_uin.get(markname)
        if not uin:
            return
        return self.send_buddy_msg(uin, content)

    def accept_verify(self, uin, account, markname=""):
        """ 同意验证请求

        :param  uin: 请求人uin
        :param account: 请求人账号
        :param markname: 添加后的备注
        """
        return self.load_next_request(AcceptVerifyRequest(uin, account,
                                                          markname))
