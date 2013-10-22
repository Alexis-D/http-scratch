# -*- coding: utf-8 -*-
import collections
import httplib
import logging
import os.path
import re
import urlparse

import eventlet

# TODO(alexis):
# - more powerful regexes


logger = logging.getLogger('scratch')
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)8s] %(message)s')


Request = collections.namedtuple(
    'Request', ['method', 'resource', 'headers', 'body', 'query'])


class ScratchException(Exception):
    def __init__(self, code, data=''):
        self.code = code
        self.data = data


class Redirect(ScratchException):
    def __init__(self, location, temporary):
        ScratchException.__init__(
            self,
            httplib.FOUND if temporary else httplib.MOVED_PERMANENTLY,
            location)


class ScratchApp(object):
    def __init__(self):
        self.routes = collections.defaultdict(collections.OrderedDict)
        self.error_handlers = collections.defaultdict(dict)

    def parse_request_line(self, line):
        splits = re.split(r'\s+', line.strip())

        if len(splits) == 2:
            raise ScratchException(httplib.HTTP_VERSION_NOT_SUPPORTED,
                                   'Request line contains 2 parts')

        elif len(splits) != 3:
            raise ScratchException(httplib.BAD_REQUEST, line)

        method, resource, http_version = splits

        if http_version != 'HTTP/1.1':
            raise ScratchException(httplib.HTTP_VERSION_NOT_SUPPORTED,
                                   http_version)

        method = method.upper()
        url = urlparse.urlparse(resource)
        resource = url.path
        query = urlparse.parse_qs(url.query)

        if method not in ['DELETE', 'GET', 'HEAD', 'POST', 'PUT']:
            raise ScratchException(httplib.METHOD_NOT_ALLOWED, method)

        return method, resource, query

    def write_response(self, sock, code, headers, response=''):
        message = httplib.responses[code]
        parts = ['HTTP/1.1 %d %s' % (code, message)]
        parts.extend(headers)
        parts.append('Content-Length: ' + str(len(response)))
        parts.append('Connection: close')
        parts.append('')
        parts.append(response)
        sock.write('\r\n'.join(parts))
        sock.close()

    def _error(self, sock, code, data=None):
        message = httplib.responses[code]
        logger_fn = logger.error if code // 100 == 5 else logger.warning
        logger_fn('%d %s %s', code, message, data)
        handler = self.error_handlers.get(code, None)
        if data or not handler:
            self.write_response(sock, code, ['Content-Type: text'], data or '')
        else:
            self.write_response(sock, code, [], handler())

    def handle_one(self, sock):
        method, resource, query = self.parse_request_line(sock.readline())
        logger.info('%s %s', method, resource)
        headers = httplib.HTTPMessage(sock)

        if 'Host' not in headers:
            raise ScratchException(
                httplib.BAD_REQUEST, 'Host header is required.')

        content_length = int(headers.get('Content-Length', 0))
        body = sock.read(content_length)
        handler = self.get_handler(method, resource)

        if not handler:
            raise ScratchException(httplib.NOT_FOUND, resource)

        handler, args = handler
        request = Request(method=method,
                          resource=resource,
                          headers=headers,
                          body=body,
                          query=query)

        try:
            response = handler(request, **args)
            logger.info('200 OK %s %s', method, resource)
            self.write_response(sock, httplib.OK, [], response)
        except Redirect as e:
            message = httplib.responses[e.code]
            logging.info(
                '%d %s %s %s → %s',
                e.code,
                message,
                method,
                resource,
                e.data)
            self.write_response(sock, e.code, ['Location: ' + e.data])

    def handle_one_safe(self, sock):
        try:
            self.handle_one(sock)
        except ScratchException as e:
            self._error(sock, e.code, e.data)
        except Exception as e:
            logging.exception('Unexpected exception')
            self._error(sock, httplib.INTERNAL_SERVER_ERROR)

    def serve_forever(self, addr, port=80):
        self.addr = addr
        self.port = port
        logger.info('Listening on %s:%d...', addr, port)
        logger.info('Hit ^C to stop the server')
        self.server = eventlet.listen((addr, port))
        pool = eventlet.GreenPool()

        while True:
            try:
                sock, addr = self.server.accept()
                pool.spawn_n(self.handle_one_safe, sock.makefile('rw'))
            except KeyboardInterrupt:
                break

    def get_handler(self, method, resource):
        for regex, handler in self.routes[method].items():
            match = regex.match(resource)
            if match:
                return handler, match.groupdict()

    def route(self, method, resource):
        logger.info('Add route %s %s', method, resource)
        regex = re.sub(r':([a-zA-Z_]\w+)', r'(?P<\1>[^/]+)', resource)
        regex = '^' + regex + '$'
        regex = re.compile(regex)

        def decorator(handler):
            self.routes[method][regex] = handler
            return handler

        return decorator

    def get(self, resource):
        return self.route('GET', resource)

    def post(self, resource):
        return self.route('POST', resource)

    def redirect(self, location, temporary=False):
        if location.startswith('/'):
            location = 'http://%s:%d%s' % (self.addr, self.port, location)

        raise Redirect(location, temporary)

    def error(self, code):
        def decorator(handler):
            self.error_handlers[code] = handler
            return handler

        return decorator

    # could handle some more stuff such as caching...
    def static(self, resource, path):
        self.route('GET', resource + '/.+')(self.serve_static(resource, path))

    def serve_static(self, resource, path):
        def decorator(request):
            ppath = os.path.abspath(path)
            filepath = os.path.join(
                ppath,
                request.resource[len(resource) + 1:])  # +1 to discard /
            filepath = os.path.abspath(filepath)
            print ppath, filepath
            if os.path.commonprefix([filepath, ppath]) != ppath:
                # someone is trying to ../..
                raise ScratchException(httplib.NOT_FOUND, request.resource)

            try:
                with open(filepath) as f:
                    return f.read()
            except IOError:
                raise ScratchException(httplib.NOT_FOUND, request.resource)

        return decorator


if __name__ == '__main__':
    app = ScratchApp()

    @app.get('/')
    def index(request):
        return 'Hello, world!'

    @app.get('/hello/:who')
    def hello(request, who):
        return 'Hello, %s!\n' % who

    @app.get('/howdy/:who')
    def howdy(request, who):
        app.redirect('/hello/' + who, True)

    @app.get('/404')
    def fourohfour(request):
        raise ScratchException(httplib.NOT_FOUND, 'Four oh four')

    @app.get('/500')
    def trigger_internal_error(request):
        1 // 0

    @app.error(httplib.INTERNAL_SERVER_ERROR)
    def internal_error_handler():
        return 'Internal error :('

    app.static('/static', '.')
    app.serve_forever('de1021.ispfr.net', 1024)
