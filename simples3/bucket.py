"""Bucket manipulation"""

import time
import hmac
import hashlib
import re
import httplib
import urllib2
import datetime
import warnings
from xml.etree import cElementTree as ElementTree
from contextlib import contextmanager
from urllib import quote_plus
from base64 import b64encode


from .utils import (_amz_canonicalize, metadata_headers, rfc822_fmt,
                    _iso8601_dt, aws_md5, aws_urlquote, guess_mimetype,
                    info_dict, expire2datetime)

amazon_s3_ns_url = "http://s3.amazonaws.com/doc/2006-03-01/"

class S3Error(Exception):
    fp = None

    def __init__(self, message, **kwds):
        self.args = message, kwds.copy()
        self.msg, self.extra = self.args

    def __str__(self):
        rv = self.msg
        if self.extra:
            rv += " ("
            rv += ", ".join("%s=%r" % i for i in self.extra.iteritems())
            rv += ")"
        return rv

    @classmethod
    def from_urllib(cls, e, **extra):
        """Try to read the real error from AWS."""
        self = cls("HTTP error", **extra)
        for attr in ("reason", "code", "filename"):
            if attr not in extra and hasattr(e, attr):
                self.extra[attr] = getattr(e, attr)
        self.fp = getattr(e, "fp", None)
        if self.fp:
            # The except clause is to avoid a bug in urllib2 which has it read
            # as in chunked mode, but S3 gives an empty reply.
            try:
                self.data = data = self.fp.read()
            except (httplib.HTTPException, urllib2.URLError), e:
                self.extra["read_error"] = e
            else:
                data = data.decode("utf-8")
                begin, end = data.find("<Message>"), data.find("</Message>")
                if min(begin, end) >= 0:
                    self.msg = data[begin + 9:end]
        return self

    @property
    def code(self): return self.extra.get("code")

class KeyNotFound(S3Error, KeyError):
    @property
    def key(self): return self.extra.get("key")

class StreamHTTPHandler(urllib2.HTTPHandler):
    pass

class StreamHTTPSHandler(urllib2.HTTPSHandler):
    pass

class AnyMethodRequest(urllib2.Request):
    def __init__(self, method, *args, **kwds):
        self.method = method
        urllib2.Request.__init__(self, *args, **kwds)

    def get_method(self):
        return self.method

class S3File(str):
    def __new__(cls, value, **kwds):
        return super(S3File, cls).__new__(cls, value)

    def __init__(self, value, **kwds):
        kwds["data"] = value
        self.kwds = kwds

    def put_into(self, bucket, key):
        return bucket.put(key, **self.kwds)

class S3Listing(object):
    """Representation of a single pageful of S3 bucket listing data."""

    truncated = None

    def __init__(self, etree):
        # TODO Use SAX - processes XML before downloading entire response
        root = etree.getroot()
        expect_tag = self._mktag("ListBucketResult")
        if root.tag != expect_tag:
            raise ValueError("root tag mismatch, wanted %r but got %r"
                             % (expect_tag, root.tag))
        self.etree = etree
        trunc_text = root.findtext(self._mktag("IsTruncated"))
        self.truncated = {"true": True, "false": False}[trunc_text]

    def __iter__(self):
        root = self.etree.getroot()
        for entry in root.findall(self._mktag("Contents")):
            item = self._el2item(entry)
            yield item
        self.next_marker = item[0]

    @classmethod
    def parse(cls, resp):
        return cls(ElementTree.parse(resp))

    def _mktag(self, name):
        return "{%s}%s" % (amazon_s3_ns_url, name)

    def _el2item(self, el):
        get = lambda tag: el.findtext(self._mktag(tag))
        key = get("Key")
        modify = _iso8601_dt(get("LastModified"))
        etag = get("ETag")
        size = int(get("Size"))
        return (key, modify, etag, size)

class S3Bucket(object):
    default_encoding = "utf-8"

    def __init__(self, name, access_key=None, secret_key=None,
                 base_url=None, timeout=None, secure=False,
                 amazon_s3_domain='s3.amazonaws.com'):
        scheme = ("http", "https")[int(bool(secure))]
        if not base_url:
            base_url = "%s://%s/%s" % (scheme, amazon_s3_domain, aws_urlquote(name))
        elif secure is not None:
            if not base_url.startswith(scheme + "://"):
                raise ValueError("secure=%r, url must use %s"
                                 % (secure, scheme))
        self.opener = self.build_opener()
        self.name = name
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = base_url
        self.timeout = timeout

    def __str__(self):
        return "<%s %s at %r>" % (self.__class__.__name__, self.name, self.base_url)

    def __repr__(self):
        return self.__class__.__name__ + "(%r, access_key=%r, base_url=%r)" % (
            self.name, self.access_key, self.base_url)

    def __getitem__(self, name): return self.get(name)
    def __delitem__(self, name): return self.delete(name)
    def __setitem__(self, name, value):
        if hasattr(value, "put_into"):
            return value.put_into(self, name)
        else:
            return self.put(name, value)
    def __contains__(self, name):
        try:
            self.info(name)
        except KeyError:
            return False
        else:
            return True

    @contextmanager
    def timeout_disabled(self):
        (prev_timeout, self.timeout) = (self.timeout, None)
        try:
            yield
        finally:
            self.timeout = prev_timeout

    @classmethod
    def build_opener(cls):
        return urllib2.build_opener(StreamHTTPHandler, StreamHTTPSHandler)

    def sign_description(self, desc):
        """AWS-style sign data."""
        key = self.secret_key.encode("ascii")
        value = desc.encode("utf-8")
        hasher = hmac.new(key, value, hashlib.sha1)
        return b64encode(hasher.digest())

    def make_description(self, method, key=None, data=None,
                         headers={}, bucket=None):
        # The signature descriptor is detalied in the developer's PDF on p. 65.
        res = self.canonicalized_resource(key, bucket=bucket)
        # Make description. :/
        return "\n".join((method, headers.get("Content-MD5", ""),
            headers.get("Content-Type", ""), headers.get("Date", ""))) + "\n" +\
            _amz_canonicalize(headers) + res

    def canonicalized_resource(self, key, bucket=None):
        res = "/"
        if bucket or bucket is None:
            res += aws_urlquote(bucket or self.name)
        res += "/"
        if key:
            res += aws_urlquote(key)
        return res

    def get_request_signature(self, method, key=None, data=None,
                              headers={}, bucket=None):
        desc = self.make_description(method, key=key, data=data,
                                     headers=headers, bucket=bucket)
        return self.sign_description(desc)

    def new_request(self, method, key=None, args=None, data=None, headers={}):
        headers = headers.copy()
        if data and "Content-MD5" not in headers:
            headers["Content-MD5"] = aws_md5(data)
        if "Date" not in headers:
            headers["Date"] = time.strftime(rfc822_fmt, time.gmtime())
        if "Authorization" not in headers:
            sign = self.get_request_signature(method, key=key, data=data,
                                              headers=headers)
            headers["Authorization"] = "AWS %s:%s" % (self.access_key, sign)
        url = self.make_url(key, args)
        return AnyMethodRequest(method, url, data=data, headers=headers)

    def make_url(self, key, args=None, arg_sep=";"):
        url = self.base_url + "/"
        if key:
            url += aws_urlquote(key)
        if args:
            if hasattr(args, "iteritems"):
                args = args.iteritems()
            args = ((quote_plus(k), quote_plus(v)) for (k, v) in args)
            url += "?" + arg_sep.join("%s=%s" % i for i in args)
        return url

    def open_request(self, request):
        if self.timeout:
            return self.opener.open(request, timeout=self.timeout)
        else:
            return self.opener.open(request)

    def make_request(self, method, key=None, args=None, data=None, headers={}):
        for retry_no in xrange(10):
            request = self.new_request(method, key=key, args=args,
                                       data=data, headers=headers)
            try:
                return self.open_request(request)
            except (urllib2.HTTPError, urllib2.URLError), e:
                # If S3 gives HTTP 500, we should try again.
                ecode = getattr(e, "code", None)
                if ecode == 500:
                    continue
                elif ecode == 404:
                    exc_cls = KeyNotFound
                else:
                    exc_cls = S3Error
                raise exc_cls.from_urllib(e, key=key)
        else:
            raise RuntimeError("ran out of retries")  # Shouldn't happen.

    def get(self, key):
        response = self.make_request("GET", key=key)
        response.s3_info = info_dict(dict(response.info()))
        return response

    def info(self, key):
        response = self.make_request("HEAD", key=key)
        rv = info_dict(dict(response.info()))
        response.close()
        return rv

    def put(self, key, data=None, acl=None, metadata={}, mimetype=None,
            transformer=None, headers={}):
        if isinstance(data, unicode):
            data = data.encode(self.default_encoding)
        headers = headers.copy()
        if mimetype:
            headers["Content-Type"] = str(mimetype)
        elif "Content-Type" not in headers:
            headers["Content-Type"] = guess_mimetype(key)
        headers.update(metadata_headers(metadata))
        if acl: headers["X-AMZ-ACL"] = acl
        if transformer: data = transformer(headers, data)
        if "Content-Length" not in headers:
            headers["Content-Length"] = str(len(data))
        if "Content-MD5" not in headers:
            headers["Content-MD5"] = aws_md5(data)
        self.make_request("PUT", key=key, data=data, headers=headers).close()

    def delete(self, key):
        # In <=py25, urllib2 raises an exception for HTTP 204, and later
        # does not, so treat errors and non-errors as equals.
        try:
            resp = self.make_request("DELETE", key=key)
        except KeyNotFound, e:
            e.fp.close()
            return False
        else:
            return 200 <= resp.code < 300

    # TODO Expose the conditional headers, x-amz-copy-source-if-*
    # TODO Add module-level documentation and doctests.
    def copy(self, source, key, acl=None, metadata=None,
             mimetype=None, headers={}):
        """Copy S3 file *source* on format '<bucket>/<key>' to *key*.

        If metadata is not None, replaces the metadata with given metadata,
        otherwise copies the previous metadata.

        Note that *acl* is not copied, but set to *private* by S3 if not given.
        """
        headers = headers.copy()
        headers.update({"Content-Type": mimetype or guess_mimetype(key)})
        headers["X-AMZ-Copy-Source"] = source
        if acl: headers["X-AMZ-ACL"] = acl
        if metadata is not None:
            headers["X-AMZ-Metadata-Directive"] = "REPLACE"
            headers.update(metadata_headers(metadata))
        else:
            headers["X-AMZ-Metadata-Directive"] = "COPY"
        self.make_request("PUT", key=key, headers=headers).close()

    def _get_listing(self, args):
        return S3Listing.parse(self.make_request("GET", args=args))

    def listdir(self, prefix=None, marker=None, limit=None, delimiter=None):
        """List bucket contents.

        Yields tuples of (key, modified, etag, size).

        *prefix*, if given, predicates `key.startswith(prefix)`.
        *marker*, if given, predicates `key > marker`, lexicographically.
        *limit*, if given, predicates `len(keys) <= limit`.

        *key* will include the *prefix* if any is given.

        .. note:: This method can make several requests to S3 if the listing is
                  very long.
        """
        m = (("prefix", prefix),
             ("marker", marker),
             ("max-keys", limit),
             ("delimiter", delimiter))
        args = dict((str(k), str(v)) for (k, v) in m if v is not None)

        listing = self._get_listing(args)
        while listing:
            for item in listing:
                yield item

            if listing.truncated:
                args["marker"] = listing.next_marker
                listing = self._get_listing(args)
            else:
                break

    def make_url_authed(self, key, expire=datetime.timedelta(minutes=5)):
        """Produce an authenticated URL for S3 object *key*.

        *expire* is a delta or a datetime on which the authenticated URL
        expires. It defaults to five minutes, and accepts a timedelta, an
        integer delta in seconds, or a datetime.

        To generate an unauthenticated URL for a key, see `B.make_url`.
        """
        # NOTE There is a usecase for having a headers argument to this
        # function - Amazon S3 will validate the X-AMZ-* headers of the GET
        # request, and so for the browser to send such a header, it would have
        # to be listed in the signature description.
        expire = expire2datetime(expire)
        expire = time.mktime(expire.timetuple()[:9])
        expire = str(int(expire))
        sign = self.get_request_signature("GET", key=key,
                                          headers={"Date": expire})
        args = (("AWSAccessKeyId", self.access_key),
                ("Expires", expire),
                ("Signature", sign))
        return self.make_url(key, args, arg_sep="&")

    def url_for(self, key, authenticated=False,
                expire=datetime.timedelta(minutes=5)):
        msg = "use %s instead of url_for(authenticated=%r)"
        dep_cls = DeprecationWarning
        if authenticated:
            warnings.warn(dep_cls(msg % ("make_url_authed", True)))
            return self.make_url_authed(key, expire=expire)
        else:
            warnings.warn(dep_cls(msg % ("make_url", False)))
            return self.make_url(key)

    def put_bucket(self, config_xml=None, acl=None):
        if config_xml:
            if isinstance(config_xml, unicode):
                config_xml = config_xml.encode("utf-8")
            headers = {"Content-Length": len(config_xml),
                       "Content-Type": "text/xml"}
        else:
            headers = {"Content-Length": "0"}
        if acl:
            headers["X-AMZ-ACL"] = acl
        resp = self.make_request("PUT", key=None, data=config_xml, headers=headers)
        resp.close()
        return resp.code == 200

    def delete_bucket(self):
        return self.delete(None)

class ReadOnlyS3Bucket(S3Bucket):
    """Read-only S3 bucket.

    Mostly useful for situations where urllib2 isn't available (e.g. Google App
    Engine), but you still want the utility functions (like generating
    authenticated URLs, and making upload HTML forms.)
    """

    def build_opener(self):
        return None
