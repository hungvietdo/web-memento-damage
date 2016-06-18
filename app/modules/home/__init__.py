import errno
import io
import json
from datetime import datetime
from hashlib import md5
from subprocess import Popen, PIPE
from threading import Thread

import os
from PIL import Image
from ext.blueprint import Blueprint, RequestHandler
from ext.memento import MementoWeb
from sqlalchemy import desc
from tornado import web

from app import database
from models import MementoModel


class Command(object):
    def __init__(self, cmd, pipe_callback):
        self.cmd = cmd
        self.process = None
        self.pipe_callback = pipe_callback

    def run(self, timeout, args=()):
        def target():
            self.process = Popen(self.cmd, stdout=PIPE, stderr=PIPE)

            stdout_thread = Thread(target=self.pipe_callback,
                               args=(self.process.stdout, ) + args)
            stdout_thread.daemon = True
            stdout_thread.start()

            stdin_thread = Thread(target=self.pipe_callback,
                                  args=(self.process.stderr, ) + args)
            stdin_thread.daemon = True
            stdin_thread.start()

            self.process.wait()

        thread = Thread(target=target)
        thread.daemon = True
        thread.start()

        thread.join(timeout)
        try: self.process.terminate()
        except: pass

        return self.process.returncode

class Memento(Blueprint):
    def __init__(self, *args, **settings):
        Blueprint.__init__(self, url_prefix='', *args, **settings)

        self.screenshot_dir = os.path.join(
            self.application.settings.get('cache_dir'), 'screenshot')
        self.log_dir = os.path.join(
            self.application.settings.get('cache_dir'), 'log')
        self.html_dir = os.path.join(
            self.application.settings.get('cache_dir'), 'html')

        try:
            os.makedirs(self.screenshot_dir)
            os.makedirs(self.log_dir)
            os.makedirs(self.html_dir)
        except OSError, e:
            if e.errno != errno.EEXIST:
                raise

    '''
    Handlers =================================================================
    '''
    class Index(RequestHandler):
        route = ['/', '/memento']

        @web.asynchronous
        def get(self, *args, **kwargs):
            self.render(".index.html")


    class CheckMemento(RequestHandler):
        route = ['/memento/check']

        @web.asynchronous
        def get(self, *args, **kwargs):
            url = self.get_query_argument('url')
            type = self.get_query_argument('type', 'URI-M')
            fresh = self.get_query_argument('fresh', 'false')

            self.render(".check.html", url=url, type=type, fresh=fresh)

    class GetUriM(RequestHandler):
        route = ['/memento/mementos']

        @web.asynchronous
        def get(self, *args, **kwargs):
            url = self.get_query_argument('uri-r')
            m = MementoWeb(url)
            memento_urls = m.find()

            self.write(json.dumps(memento_urls))
            self.finish()

    class Screenshot(RequestHandler):
        route = ['/memento/screenshot']

        @web.asynchronous
        def get(self, *args, **kwargs):
            url = self.get_query_argument('url')
            hashed_url = md5(url).hexdigest()

            screenshot_file = '{}.png'.format(os.path.join(
                self.blueprint.screenshot_dir, hashed_url))
            f = Image.open(screenshot_file)
            o = io.BytesIO()
            f.save(o, format="JPEG")
            s = o.getvalue()

            self.set_header('Content-type', 'image/png')
            self.set_header('Content-length', len(s))
            self.write(s)
            self.finish()

    class ProgressCheckDamage(RequestHandler):
        route = ['/memento/damage/progress']

        @web.asynchronous
        def get(self, *args, **kwargs):
            uri = self.get_query_argument('uri')
            start = self.get_query_argument('start', 0)
            start = int(start)

            hashed_uri = md5(uri).hexdigest()

            crawler_log_file = '{}.crawl.log'.format(os.path.join(
                self.blueprint.log_dir, hashed_uri))

            with open(crawler_log_file, 'rb') as f:
                for idx, line in enumerate(f.readlines()):
                    if idx >= start:
                        self.write(line)
                self.finish()


    class CheckDamage(RequestHandler):
        route = ['/memento/damage']

        @web.asynchronous
        def get(self, *args, **kwargs):
            uri = self.get_query_argument('uri')
            fresh = self.get_query_argument('fresh', 'false')
            # since all query string arguments are in unicode, cast fresh to
            # boolean
            if fresh.lower() == 'true': fresh = True
            elif fresh.lower() == 'false': fresh = False

            hashed_url = md5(uri).hexdigest()

            # If fresh == True, do fresh calculation
            if fresh:
                self.do_fresh_calculation(uri, hashed_url)
            else:
                # If there are calculation history, use it
                last_calculation = self.check_calculation_archives(hashed_url)
                if last_calculation:
                    result = last_calculation.result
                    time = last_calculation.response_time

                    result = json.loads(result)
                    result['is_archive'] = True
                    result['archive_time'] = time.isoformat()

                    self.write(result)
                    self.finish()
                else:
                    self.do_fresh_calculation(uri, hashed_url)

        def check_calculation_archives(self, hashed_uri):
            last_calculation = database.session.query(MementoModel)\
                .filter(MementoModel.hashed_uri==hashed_uri)\
                .order_by(desc(MementoModel.response_time))\
                .first()

            return last_calculation

        def do_fresh_calculation(self, uri, hashed_url):
            # Instantiate MementoModel
            model = MementoModel()
            model.uri = uri
            model.hashed_uri = hashed_url
            model.request_time = datetime.now()

            # Define path for each arguments of crawl.js
            crawljs_script = os.path.join(
                self.application.settings.get('base_dir'),
                'ext', 'phantomjs', 'crawl.js'
            )
            # Define damage.py location
            damage_py_script = os.path.join(
                self.application.settings.get('base_dir'),
                'ext', 'damage.py'
            )

            # Define arguments
            screenshot_file = '{}.png'.format(os.path.join(
                self.blueprint.screenshot_dir, hashed_url))
            html_file = '{}.html'.format(os.path.join(
                self.blueprint.html_dir, hashed_url))
            log_file = '{}.log'.format(os.path.join(
                self.blueprint.log_dir, hashed_url))
            images_log_file = '{}.img.log'.format(os.path.join(
                self.blueprint.log_dir, hashed_url))
            csses_log_file = '{}.css.log'.format(os.path.join(
                self.blueprint.log_dir, hashed_url))
            crawler_log_file = '{}.crawl.log'.format(os.path.join(
                self.blueprint.log_dir, hashed_url))

            # Logger
            f = open(crawler_log_file, 'w')
            f.write('')
            f.close()

            f = open(crawler_log_file, 'a')

            def log_output(out, page=None):
                def write(line):
                    f.write(line + '\n')
                    print(line)

                    if 'background_color' in line and page:
                        page['background_color'] = json.loads(line)\
                                                   ['background_color']
                    if 'result' in line:
                        line = json.loads(line)

                        result = line['result']
                        result['is_success'] = True
                        result['is_archive'] = False

                        result = json.dumps(result)

                        # Write result to db
                        model.response_time = datetime.now()
                        model.result = result
                        database.session.add(model)
                        database.session.commit()

                        self.write(result)
                        self.finish()

                if out and hasattr(out, 'readline'):
                    for line in iter(out.readline, b''):
                    #for line in out.readlines():
                        line =  line.strip()
                        write(line)
                else:
                    write(out)

            # Error
            def result_error(err = 'Request time out'):
                if not self._finished:
                    result = {
                        'is_success' : False,
                        'error' : err
                    }

                    self.write(json.dumps(result))
                    self.finish()

            # page background-color will be set from phantomjs result
            page = {
                'background_color': 'FFFFFF'
            }

            # Crawl page with phantomjs crawl.js via arguments
            # Equivalent with console:
            #   phantomjs crawl.js <screenshot> <html> <log>
            cmd = Command(['phantomjs', crawljs_script, uri,screenshot_file,
                           html_file, log_file], log_output)
            err_code = cmd.run(10 * 60, args=(page, ))

            if err_code == 0:
                # Calculate damage with damage.py via arguments
                # Equivalent with console:
                #   python damage.py <img_log> <css_log> <screenshot_log> <bg>
                cmd = Command(['python', damage_py_script, images_log_file,
                               csses_log_file, screenshot_file,
                               page['background_color']],
                              log_output)
                err_code = cmd.run(10 * 60)
                if err_code != 0:
                    result_error()
            else:
                result_error()

