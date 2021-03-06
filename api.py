from base64 import b64encode
from jinja2 import Template
import logging
import os
import requests
import sys
import time
import pickle
from xml.etree import ElementTree

DEFAULT_API_KEY = 'd45fd466-51e2-4701-8da8-04351c872236'
DEFAULT_API_SECRET = '171e8465-f548-401d-b63b-caf0dc28df5f'
DEFAULT_API_URL = 'http://www.betafaceapi.com/service.svc'
DEFAULT_POLL_INTERVAL = 1

def we_are_frozen():
    # All of the modules are built-in to the interpreter, e.g., by py2exe
    return hasattr(sys, "frozen")

def module_path():
    encoding = sys.getfilesystemencoding()
    if we_are_frozen():
        return os.path.dirname(unicode(sys.executable, encoding))
    return os.path.dirname(unicode(__file__, encoding))

class BetaFaceAPI(object):

    def __init__(self, **kwargs):
        self.api_key = kwargs.get('api_key', DEFAULT_API_KEY)
        self.api_secret = kwargs.get('api_secret', DEFAULT_API_SECRET)
        self.api_url = kwargs.get('api_url', DEFAULT_API_URL)
        self.poll_interval = kwargs.get('poll_interval', DEFAULT_POLL_INTERVAL)
        self.cache = kwargs.get('cache', '')
        self.logger = logging.getLogger(self.__class__.__name__)

    def upload_face(self, file_name, person_id):
        """ Uploads an image to BetaFace API, waits for it to be processed
            by polling each poll_interval seconds, and then assigns a person_id
            (alpha-numberic + '.') to that image. """

        # Step 1: Encode image in base 64, upload it to service and get image ID
        file_contents = open(file_name, "rb").read()
        params = {
            'base64_data': b64encode(file_contents),
            'original_filename': file_name
        }
        result = self._api_call('UploadNewImage_File', params)
        if result is None:
            self.logger.error("API call to upload image failed!")
            return None

        # Step 2: keep polling the GetImageInfo endpoint until the processing
        # of the uploaded image is ready.
        img_uid = result['img_uid']
        result = self._api_call('GetImageInfo', {'image_uid': img_uid})
        while not result['ready']:
            time.sleep(self.poll_interval)
            result = self._api_call('GetImageInfo', {'image_uid': img_uid})
        if 'face_uid' in result:
            face_uid = result['face_uid']
        else:
            return

        # Step 3: associate the face with the person via Faces_SetPerson endpoint
        params = {
            'face_uid': face_uid,
            'person_id': person_id
        }
        result = self._api_call('SetPerson', params)

        if not result['ready']:
            return None
        else:
            return result

    def get_image_info(self, img_uid):

        result = self._api_call('GetImageInfo', {'image_uid': img_uid})
        if result is None:
            return None
        
        while not result['ready']:
            time.sleep(self.poll_interval)
            result = self._api_call('GetImageInfo', {'image_uid': img_uid})

        return result

    def recognize_faces(self, file_name, namespace):
        # Step 1: Encode image in base 64, upload it to service and get image ID
        file_contents = open(file_name, "rb").read()
        params = {
            'base64_data': b64encode(file_contents),
            'original_filename': file_name
        }
        result = self._api_call('UploadNewImage_File', params)
        if result is None:
            self.logger.error("API call to upload image failed!")
            return None

        # Step 2: keep polling the GetImageInfo endpoint until the processing
        # of the uploaded image is ready.
        img_uid = result['img_uid']
        result = self._api_call('GetImageInfo', {'image_uid': img_uid})
        while not result['ready']:
            time.sleep(self.poll_interval)
            result = self._api_call('GetImageInfo', {'image_uid': img_uid})
        if 'face_uid' in result:
            face_uid = result['face_uid']
        else:
            return {}

        # Step 3: Start a face recognition job
        if '@' not in namespace:
            namespace = 'all@' + namespace
        params = {'face_uid': face_uid, 'namespace': namespace}
        result = self._api_call('RecognizeFaces', params)
        if not result['ready']:
            self.logger.error('RecognizeFaces returned int_response != 0')
            return None

        # Step 4: Wait for the recognition job to finish
        params = {'recognize_job_id': result['recognize_job_id']}
        result = self._api_call('GetRecognizeResult', params)
        while not result['ready']:
            time.sleep(self.poll_interval)
            result = self._api_call('GetRecognizeResult', params)

        return result['matches']

    def dict_to_str(self, d):
        s = ''
        for i in d:
            if i == 'base64_data':
                v = '[BLOB]'
            else:
                v = d[i]
            v = v.replace('/','-')
            s += '{}={}&'.format(i,v[:254])
        return s[:-1]

    def get_cache_file_name(self, endpoint, params):
        if endpoint not in ['GetRecognizeResult', 'GetImageInfo']: # Use to poll for state
            return '{}/{}?{}'.format(self.cache, endpoint, self.dict_to_str(params))
        else:
            return None

    def get_api_result_from_cache(self, endpoint, params):
        if self.cache != '':
            cache_file = self.get_cache_file_name(endpoint, params)

            if cache_file and os.path.isfile(cache_file):
                self.logger.info('Using cached file {}'.format(cache_file))
                return pickle.load(open(cache_file,'rb'))
            else:
                return None
        else:
            return None


    def set_api_result_to_cache(self, endpoint, params, result):
        if self.cache != '':
            cache_file = self.get_cache_file_name(endpoint, params)
            if cache_file:
                pickle.dump( result, open(cache_file, 'w' ))

        return result

    def _api_call(self, endpoint, params):
        """ Make an API call to a given endpoint, with given params.

        This will actually fetch the template from request_templates/endpoint,
        render it to a string using jinja2 templating engine, POST the
        data using content_type = application/xml to the BetaFace API,
        fetch the response and possibly parse it if there is a function
        available.

        Returns a dictionary of parsed stuff from the response, or None
        if the request failed.

        """

        cached = self.get_api_result_from_cache(endpoint, params)
        if cached:
            return cached

        api_call_params = {
            'api_key': self.api_key,
            'api_secret': self.api_secret
        }
        api_call_params.update(params)

        template_name = "%s/request_templates/%s.xml" % (module_path(), endpoint)
        request_data = self._render_template(template_name, api_call_params)
        url = self.api_url + '/' + endpoint
        self.logger.info("Making HTTP request to %s" % url)
        if endpoint != 'UploadNewImage_File':
            self.logger.debug("Making HTTP request with body:\n%s" % request_data)
        headers = {'content-type': 'application/xml'}
        request = requests.post(url, data = request_data, headers = headers)
        # If HTTP request failed, bail out
        if request.status_code != 200:
            self.logger.error("HTTP request failed with status code %d" %
                              request.status_code)
            request.raise_for_status() # Communicate error to the client, so he can react.
            return request # If no error to raise, but still !=200, share the request object 

        result = {'raw_content': request.text}
        if endpoint != 'GetImageInfo':
            self.logger.debug('Response:\n' + request.content)

        request_parser = getattr(self, '_parse_%s' % endpoint, None)
        if request_parser is not None:
            self.logger.info("Using custom response parser for endpoint %s" %
                             endpoint)
            tree = ElementTree.fromstring(request.content)
            try:
                parsed_result = request_parser(tree)
            except Exception, e:
                self.logger.error("Error while parsing response: %r" % e)
                return None

            if parsed_result is None:
                self.logger.error("Custom parsing failed for endpoint %s" %
                                  endpoint)
                return None

            result.update(parsed_result)

        return self.set_api_result_to_cache(endpoint, params, result)

    def _render_template(self, template_file, context):
        """ Renders a template to a string, given context vars. """

        with open(template_file, 'rt') as src:
            content = ''.join(src.readlines())
            jinja_template = Template(content)
            return jinja_template.render(**context)

    def _parse_UploadNewImage_File(self, response):
        """ Parse the upload new image file response. """
        result = {}

        img_uid = response.findall('.//img_uid')
        if len(img_uid) == 0:
            return None
        result['img_uid'] = img_uid[0].text

        ready = response.findall('.//int_response')
        if len(ready) == 0:
            return None
        result['ready'] = (ready[0].text.strip() == '0')

        return result

    def _parse_SetPerson(self, response):
        """ Parse the get image info response. """
        result = {}

        ready = response.findall('.//int_response')
        if len(ready) == 0:
            return None
        result['ready'] = (ready[0].text.strip() == '0')

        return result

    def _parse_GetImageInfo(self, response):
        """ Parse the get image info response. """
        result = {}

        ready = response.findall('.//int_response')
        if len(ready) == 0:
            return None
        result['ready'] = (ready[0].text.strip() == '0')

        # If not ready yet, stop parsing at 'ready'
        if not result['ready']:
            return result

        # Otherwise, see if we have faces
        face_uids = response.findall('.//faces/FaceInfo/uid')
        if len(face_uids) == 0:
            self.logger.info("No faces found in image!")
            return result
        result['face_uid'] = face_uids[0].text

        return result

    def _parse_RecognizeFaces(self, response):
        """ Parse the Faces_Recognize result. """
        result = {}

        ready = response.findall('.//int_response')
        if len(ready) == 0:
            return None
        result['ready'] = (ready[0].text.strip() == '0')

        # If not ready yet, stop parsing at 'ready'
        if not result['ready']:
            return result

        recognize_job_id = response.findall('.//recognize_uid')
        if len(recognize_job_id) == 0:
            return None
        result['recognize_job_id'] = recognize_job_id[0].text

        return result

    def _parse_GetRecognizeResult(self, response):
        result = {}

        ready = response.findall('.//int_response')
        if len(ready) == 0:
            return None
        result['ready'] = (ready[0].text.strip() == '0')

        # If not ready yet, stop parsing at 'ready'
        if not result['ready']:
            return result

        matching_persons = response.findall('.//faces_matches/FaceRecognizeInfo/matches/PersonMatchInfo')
        if len(matching_persons) == 0:
            self.logger.info("No matching persons found for image!")
            return result

        result['matches'] = {}
        for matching_person in matching_persons:
            person_name = matching_person.find('person_name').text
            confidence = float(matching_person.find('confidence').text)
            result['matches'][person_name] = confidence

        return result
