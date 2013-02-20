# Copyright (C) 2009-2013 Sebastian Rahlf <basti at redtoad dot de>
#
# This program is release under the BSD License. You can find the full text of
# the license in the LICENSE file.

__docformat__ = "restructuredtext en"

from base64 import b64encode
from datetime import datetime, timedelta
import gzip
import hmac
import socket
import StringIO
import sys
from time import strftime, gmtime, sleep
import urllib2
import warnings

# For historic reasons, this module also supports Python 2.4. To make this
# happen, a few things have to be imported differently, e.g. pycrypto is needed
# to create URL signatures.
if sys.version_info[:2] > (2, 4): # pragma: no cover
    from urllib2 import quote
    from hashlib import sha256 # pylint: disable-msg=E0611
else:
    from urllib import quote
    from Crypto.Hash import SHA256 as sha256

    # builtin function all() is only available from Python 2.5 onward!
    def all(iterable):
        """
        Returns True if all elements of the iterable are true (or if the
        iterable is empty).
        """
        for element in iterable:
            if not element:
                return False
        return True

from amazonproduct.version import VERSION
from amazonproduct.errors import *
from amazonproduct.utils import load_config, load_class
from amazonproduct.utils import running_on_gae, REQUIRED_KEYS
from amazonproduct.processors import ITEMS_PAGINATOR, BaseProcessor


# first processors successfully imported is used
PROCESSORS = [
    'amazonproduct.processors.objectify.Processor',
    'amazonproduct.processors.etree.Processor',
    'amazonproduct.processors.minidom.Processor',
]


USER_AGENT = ('python-amazon-product-api/%s '
    '+http://pypi.python.org/pypi/python-amazon-product-api/' % VERSION)


#: Hosts used by Amazon for normal/XSLT operations
HOSTS = {
    'ca': 'ecs.amazonaws.ca',
    'cn': 'webservices.amazon.cn',
    'de': 'ecs.amazonaws.de',
    'es': 'webservices.amazon.es',
    'fr': 'ecs.amazonaws.fr',
    'it': 'webservices.amazon.it',
    'jp': 'ecs.amazonaws.jp',
    'uk': 'ecs.amazonaws.co.uk',
    'us': 'ecs.amazonaws.com',
}


class GZipHandler(urllib2.BaseHandler):

    """
    A handler to deal with gzip encoded content.
    Borrowed from Andrew Rowls
    http://techknack.net/python-urllib2-handlers/
    """

    def http_request(self, req):
        req.add_header('Accept-Encoding', 'gzip')
        return req

    def http_response(self, req, resp):
        if resp.headers.get('content-encoding') == 'gzip':
            gz = gzip.GzipFile(fileobj=StringIO.StringIO(resp.read()), mode='r')
            old = resp
            resp = urllib2.addinfourl(gz, old.headers, old.url)
            resp.msg = old.msg
            resp.code = old.code # support for Python2.4/2.5
        return resp

    https_request = http_request
    https_response = http_response


class API (object):

    """
    Wrapper class for the Amazon Product Advertising API.

    You will need an *AWS access key*, its *secret counterpart* and an
    *associate ID*. Create the file ``~/.amazon-product-api`` and add the
    following content::

        [Credentials]
        access_key = <your access key>
        secret_key = <your secret key>
        associate_tag = <your associate id>

    Now you can use this class to do things like this (note lxml must be
    installed for this to work) ::
        
        from amazonproduct import API
        api = API(locale='us')
        root = api.item_lookup('0136042597', IdType='ISBN',
                    SearchIndex='Books', ResponseGroup='Reviews', ReviewPage=1)

        reviews_iframe = root.Items.Item.CustomerReviews.IFrameURL
    """

    VERSION = '2011-08-01' #: supported Amazon API version
    REQUESTS_PER_SECOND = 1 #: max requests per second
    TIMEOUT = 5 #: timeout in seconds

    def __init__(self, access_key_id=None, secret_access_key=None, locale=None,
                 associate_tag=None, processor=None):
        """
        .. versionchanged:: 0.2.6
           Passing parameters ``access_key_id``, ``secret_access_key`` and
           ``associate_tag`` directly to the constructor will be removed in one
           of the next releases. See :ref:`config` for alternatives.

        :param access_key_id: AWS access key ID (deprecated).
        :param secret_key_id: AWS secret key (deprecated).
        :param associate_tag: Amazon Associates tracking id (deprecated).
        :param locale: localise results by using one value from ``LOCALES``.
        :param processor: result processing function (``None`` if unsure).
        """
        if not (access_key_id is None and secret_access_key is None
        and associate_tag is None):
            warnings.warn('Please use a config file!', DeprecationWarning,
                stacklevel=2)

        self.access_key = access_key_id
        self.secret_key = secret_access_key
        self.associate_tag = associate_tag
        self.locale = locale

        # load missing valued from config file
        if not all(getattr(self, key, False) for key in REQUIRED_KEYS):
            cfg = load_config()
            for key in REQUIRED_KEYS:
                if getattr(self, key, '???') is None and cfg.get(key, None):
                    setattr(self, key, cfg[key])

        try:
            self.host = HOSTS[self.locale]
        except KeyError:
            raise UnknownLocale(locale)


        # GAE does not allow timeouts to be specified manually
        if not running_on_gae():
            socket.setdefaulttimeout(self.TIMEOUT)

        self.last_call = datetime(1970, 1, 1)
        self.debug = 0 # set to 1 if you want to see HTTP headers

        if isinstance(processor, BaseProcessor):
            self.processor = processor
        else:
            self.processor = self._load_processor(processor)()

    @staticmethod
    def _load_processor(*names):
        """
        Loads result processor. If no processor is given (``None``), the first
        one is taken that can be successfully imported from the list of default
        processors (:const:`PROCESSORS`).
        """
        if len(names) == 0 or names[0] is None:
            names = PROCESSORS
        for name in names:
            # processor was given as string
            if isinstance(name, (str, unicode)):
                try:
                    pclass = load_class(name)
                    if issubclass(pclass, BaseProcessor):
                        return pclass
                except ImportError:
                    continue
            # processor was given as class
            elif isinstance(name, type) and issubclass(name, BaseProcessor):
                return name
        # nothing successfully loaded
        raise ImportError('No processor class could be imported!')

    def __repr__(self):
        return '<API(%s/%s) at %s>' % (self.VERSION, self.locale, hex(id(self)))

    def _build_url(self, **qargs):
        """
        Builds a signed URL for querying Amazon AWS.  This function is based
        on code by Adam Cox (found at
        http://blog.umlungu.co.uk/blog/2009/jul/12/pyaws-adding-request-authentication/)
        """
        # remove empty (=None) parameters
        for key, val in qargs.items():
            if val is None:
                del qargs[key]

        if 'AWSAccessKeyId' not in qargs:
            qargs['AWSAccessKeyId'] = self.access_key

        if 'Service' not in qargs:
            qargs['Service'] = 'AWSECommerceService'

        # use the version this class was build for by default
        if 'Version' not in qargs:
            qargs['Version'] = self.VERSION

        if 'AssociateTag' not in qargs and self.associate_tag:
            qargs['AssociateTag'] = self.associate_tag

        # add timestamp (this is required when using a signature)
        qargs['Timestamp'] = strftime("%Y-%m-%dT%H:%M:%SZ", gmtime())

        # create signature
        keys = sorted(qargs.keys())
        args = '&'.join('%s=%s' % (key, quote(unicode(qargs[key])
                        .encode('utf-8'), safe='~')) for key in keys)

        msg = 'GET'
        msg += '\n' + self.host
        msg += '\n/onca/xml'
        msg += '\n' + args

        signature = quote(
            b64encode(hmac.new(self.secret_key or '', msg, sha256).digest()))

        return 'http://%s/onca/xml?%s&Signature=%s' % (
            self.host, args, signature)

    def _fetch(self, url):
        """
        Calls the Amazon Product Advertising API and returns the response.
        """
        request = urllib2.Request(url)
        request.add_header('User-Agent', USER_AGENT)

        # Be nice and wait for some time
        # before submitting the next request
        delta = datetime.now() - self.last_call
        throttle = timedelta(seconds=1/self.REQUESTS_PER_SECOND)
        if delta < throttle:
            wait = throttle-delta
            sleep(wait.seconds+wait.microseconds/1000000.0) # pragma: no cover
        self.last_call = datetime.now()

        handler = urllib2.HTTPHandler(debuglevel=self.debug)
        opener = urllib2.build_opener(handler, GZipHandler())
        response = opener.open(request)
        return response

    def _reg(self, key):
        """
        Returns the appropriate regular expression (compiled) to parse an error
        message depending on the current locale.
        """
        if self.locale == 'jp':
            return JAPANESE_ERROR_REGS[key]
        return DEFAULT_ERROR_REGS[key]

    def _parse(self, fp):
        """
        Processes the AWS response (file like object). XML is fed in, some
        usable output comes out. It will use a different result_processor if
        you have defined one.
        """
        try:
            return self.processor.parse(fp)
        except AWSError, e:

            if e.code == 'InternalError':
                raise InternalError

            if e.code == 'InvalidClientTokenId':
                raise InvalidClientTokenId

            if e.code == 'MissingClientTokenId':
                raise MissingClientTokenId

            if e.code == 'AWS.MissingParameters':
                m = self._reg('missing-parameters').search(e.msg)
                raise MissingParameters(m.group('parameter'))

            if e.code == 'RequestThrottled':
                raise TooManyRequests

            if e.code == 'Deprecated':
                raise DeprecatedOperation(e.msg)

            if e.code == 'AWS.ECommerceService.NoExactMatches':
                raise NoExactMatchesFound

            if e.code == 'AWS.ECommerceService.NoExactMatches':
                raise NoExactMatchesFound

            if e.code == 'AWS.InvalidEnumeratedParameter':
                m = self._reg('invalid-value').search(e.msg)
                if m is not None:
                    if m.group('parameter') == 'ResponseGroup':
                        raise InvalidResponseGroup()
                    elif m.group('parameter') == 'SearchIndex':
                        raise InvalidSearchIndex()

            if e.code == 'AWS.InvalidParameterValue':
                m = self._reg('invalid-parameter-value').search(e.msg)
                raise InvalidParameterValue(m.group('parameter'),
                                            m.group('value'))

            if e.code == 'AWS.RestrictedParameterValueCombination':
                m = self._reg('invalid-parameter-combination').search(e.msg)
                raise InvalidParameterCombination(m.group('message'))

            if e.code == 'AccountLimitExceeded':
                raise AccountLimitExceeded(e.msg)

            # otherwise simply re-raise
            raise

    def call(self, **qargs):
        """
        Builds a signed URL for the operation, fetches the result from Amazon
        and parses the XML.

        Example::

            xml = api.call(Operation='ItemLookup', ItemId='B067884223')

        .. note:: If you want to customise things at any stage, simply override the respective method(s):

        * ``_build_url(**query_parameters)``
        * ``_fetch(url)``
        * ``_parse(fp)``
        """
        url = self._build_url(**qargs)
        try:
            fp = self._fetch(url)
            return self._parse(fp)
        except urllib2.HTTPError, e:
            # Some HTTP send a more detailed error message as body which can be
            # parsed too.
            # - 400 (Bad Request)
            # - 403 (Unauthorised)
            # - 410 (Gone)
            # - 503 (Service unavailable)
            if e.code in (400, 403, 410, 503):
                return self._parse(e.fp)
            if e.code == 500:
                raise InternalError
            raise

    def item_lookup(self, *ids, **params):
        """
        Given an Item identifier, the ``ItemLookup`` operation returns some or
        all of the item attributes, depending on the response group specified
        in the request. By default, ``ItemLookup`` returns an item's ``ASIN``,
        ``DetailPageURL``, ``Manufacturer``, ``ProductGroup``, and ``Title`` of
        the item.

        ``ItemLookup`` supports many response groups, so you can retrieve many
        different kinds of product information, called item attributes,
        including product reviews, variations, similar products, pricing,
        availability, images of products, accessories, and other information.

        To look up more than one item at a time, separate the item identifiers
        by commas.
        """
        try:
            paginate = params.pop('paginate', False)
            operators = {
                'Operation': 'ItemLookup',
                'ItemId': ','.join(ids),
            }
            operators.update(params)

            paginator = self.processor.load_paginator(paginate)
            if paginator is not None:
                # Amazon limits returned pages to max 10 pages max
                if operators.get('limit', 10) > 10:
                    operators['limit'] = 10
                return paginator(self.call, **operators)
            else:
                return self.call(**operators)
        except AWSError, e:

            if (e.code == 'AWS.InvalidEnumeratedParameter'
            and self._reg('invalid-value').search(e.msg)
                    .group('parameter') == 'SearchIndex'):
                raise InvalidSearchIndex(params.get('SearchIndex'))

            if e.code == 'AWS.InvalidResponseGroup':
                raise InvalidResponseGroup(params.get('ResponseGroup'))

            # otherwise re-raise exception
            raise # pragma: no cover

    def item_search(self, search_index, paginate=ITEMS_PAGINATOR, **params):
        """
        The ``ItemSearch`` operation returns items that satisfy the search
        criteria, including one or more search indices.

        ``ItemSearch`` returns up to ten search results at a time. When
        ``condition`` equals "All," ``ItemSearch`` returns up to three offers
        per condition (if they exist), for example, three new, three used,
        three refurbished, and three collectible items. Or, for example, if
        there are no collectible or refurbished offers, ``ItemSearch`` returns
        three new and three used offers.

        Because there are thousands of items in each search index,
        ``ItemSearch`` requires that you specify the value for at least one
        parameter in addition to a search index. The additional parameter value
        must reference items within the specified search index. For example,
        you might specify a browse node (BrowseNode is an ``ItemSearch``
        parameter), Harry Potter Books, within the Books product category. You
        would not get results, for example, if you specified the search index
        to be Automotive and the browse node to be Harry Potter Books. In this
        case, the parameter value is not associated with the search index
        value.

        The ``ItemPage`` parameter enables you to return a specified page of
        results. The maximum ``ItemPage`` number that can be returned is 400.
        An error is returned if you try to access higher numbered pages. If you
        do not include ``ItemPage`` in your request, the first page will be
        returned by default. There can be up to ten items per page.

        ``ItemSearch`` is the operation that is used most often in requests. In
        general, when trying to find an item for sale, you use this operation.

        .. versionchanged:: 2011-08-01
           You can only fetch up to 10 result pages (instead of 400).
        """
        try:
            operators = {
                'Operation': 'ItemSearch',
                'SearchIndex': search_index,
            }
            operators.update(params)

            paginator = self.processor.load_paginator(paginate)
            if paginator is not None:
                # Amazon limits returned pages to max 5
                # if SearchIndex "All" is used!
                if search_index == 'All' and operators.get('limit', 10) > 5:
                    operators['limit'] = 5
                # otherwise it's 10 pages max
                elif operators.get('limit', 10) > 10:
                    operators['limit'] = 10
                return paginator(self.call, **operators)
            else:
                return self.call(**operators)
        except AWSError, e:

            if (e.code == 'AWS.InvalidEnumeratedParameter'
            and self._reg('invalid-value').search(e.msg)):
                raise InvalidSearchIndex(search_index)

            if e.code == 'AWS.InvalidResponseGroup':
                raise InvalidResponseGroup(params.get('ResponseGroup'))

            # otherwise re-raise exception
            raise # pragma: no cover

    def similarity_lookup(self, *ids, **params):
        """
        The ``SimilarityLookup`` operation returns up to ten products per page
        that are similar to one or more items specified in the request. This
        operation is typically used to pique a customer's interest in buying
        something similar to what they've already ordered.

        If you specify more than one item, ``SimilarityLookup`` returns the
        intersection of similar items each item would return separately.
        Alternatively, you can use the ``SimilarityType`` parameter to return
        the union of items that are similar to any of the specified items. A
        maximum of ten similar items are returned; the operation does not
        return additional pages of similar items. If there are more than ten
        similar items, running the same request can result in different answers
        because the ten that are included in the response are picked randomly.
        The results are picked randomly only when you specify multiple items
        and the results include more than ten similar items.
        """
        item_id = ','.join(ids)
        try:
            return self.call(Operation='SimilarityLookup',
                              ItemId=item_id, **params)
        except AWSError, e:

            if e.code == 'AWS.ECommerceService.NoSimilarities':
                asin = self._reg('no-similarities').search(e.msg).group('ASIN')
                raise NoSimilarityForASIN(asin)

            # otherwise re-raise exception
            raise # pragma: no cover

    def browse_node_lookup(self, browse_node_id, response_group=None, **params):
        """
        Given a ``browse_node_id``, this method returns the specified browse
        node's name, children, and ancestors. The names and browse node IDs of
        the children and ancestor browse nodes are also returned.
        :meth:`browse_node_lookup` enables you to traverse the browse node
        hierarchy to find a browse node.

        As you traverse down the hierarchy, you refine your search and limit
        the number of items returned. For example, you might traverse the
        following hierarchy: ``Books>Children's Books>Science``, to select out
        of all the science books offered by Amazon only those that are
        appropriate for children::

            >>> api = API(locale='us')
            >>> node_id = 3207 # Books > Children's Books > Science
            >>> result = api.browse_node_lookup(node_id)
            >>> for child in result.BrowseNodes.BrowseNode.Children.BrowseNode:
            ...     print '%s (%sa)' % (child.Name, child.BrowseNodeId)
            ...
            Agriculture (3208)
            Anatomy & Physiology (3209)
            Astronomy & Space (3210)
            Biology (3214)
            Botany (3215)
            Chemistry (3216)
            Earth Sciences (3217)
            Electricity & Electronics (3220)
            Engineering (16244041)
            Environment & Ecology (3221)
            Experiments & Projects (3224)
            Geography (16244051)
            Health (3230)
            Heavy Machinery (3249)
            How Things Work (3250)
            Inventions & Inventors (16244711)
            Light & Sound (16244701)
            Math (3253)
            Mystery & Wonders (15356851)
            Nature (3261)
            Physics (3283)
            Social Science (3143)
            Zoology (3301)
        
        Returning the items associated with children's science books produces a
        much more targeted result than a search based at the level of books.

        Alternatively, by traversing up the browse node tree, you can determine
        the root category of an item. You might do that, for example, to return
        the top seller of the root product category using the ``TopSellers``
        response group in an :meth:`browse_node_lookup` request::

            >>> # extract all category roots
            >>> result = api.item_lookup('031603438X', # Keith Richards: Life
            ...     ResponseGroup='BrowseNodes')
            >>> root_ids = result.xpath(
            ...     '//aws:BrowseNode[aws:IsCategoryRoot=1]/aws:BrowseNodeId',
            ...     namespaces={'aws': result.nsmap.get(None)})

            >>> # TopSellers for first category
            >>> result = api.browse_node_lookup(root_ids[0], 'TopSellers')
            >>> for item in result.BrowseNodes.BrowseNode.TopSellers.TopSeller:
            ...     print item.ASIN, item.Title
            ...
            B004LLHE62 Ghost in the Polka Dot Bikini (A Ghost of Granny Apples Mystery)
            B004LROUNG The Litigators
            B005K0HDGE 11/22/63 [Enhanced eBook]
            B004W2UBYW Steve Jobs
            1419702238 Diary of a Wimpy Kid: Cabin Fever
            1451648537 Steve Jobs
            B003YL4LNY Inheritance (The Inheritance Cycle)
            0375856110 Inheritance (The Inheritance Cycle)
            B005IGVS6Q Unfinished Business
            B005O548QI Last Breath

        You can use :meth:`browse_node_lookup` iteratively to navigate through
        the browse node hierarchy to reach the node that most appropriately
        suits your search. Then you can use the browse node ID in an
        :meth:`item_search` request. This response would be far more targeted
        than, for example, searching through all of the browse nodes in a
        search index.

        A list of BrowseNodes can be found here:
        http://docs.amazonwebservices.com/AWSECommerceService/latest/DG/index.html?BrowseNodeIDs.html

        :param browse_node_id: A positive integer assigned by Amazon that
          uniquely identifies a product category.
        :type browse_node_id: positive int

        :param response_group: Specifies the types of values to return. You can
          specify multiple response groups in one request by separating them
          with commas. Valid Values are ``BrowseNodeInfo`` (default),
          ``MostGifted``, ``NewReleases``, ``MostWishedFor``, ``TopSellers``.
        :type response_group: str

        :param params: This can be any (or none) of the
          :ref:`common-request-parameters`.
        """
        try:
            return self.call(Operation='BrowseNodeLookup',
                    BrowseNodeId=browse_node_id, ResponseGroup=response_group,
                    **params)
        except AWSError, e:

            if e.code == 'AWS.InvalidResponseGroup':
                raise InvalidResponseGroup(params.get('ResponseGroup'))

            # otherwise re-raise exception
            raise # pragma: no cover

    def _convert_cart_items(self, items, key='ASIN'):
        """
        Converts items into correct format for cart operations.
        """
        result = {}
        # TODO ListItemId
        if type(items) == dict:
            for no, (item_id, quantity) in enumerate(items.items()):
                result['Item.%i.%s' % (no+1, key)] = item_id
                result['Item.%i.Quantity' % (no+1)] = quantity
        return result

    def cart_create(self, items, **params):
        """
        The ``CartCreate`` operation enables you to create a remote shopping
        cart.  A shopping cart is the metaphor used by most e-commerce
        solutions. It is a temporary data storage structure that resides on
        Amazon servers.  The structure contains the items a customer wants to
        buy. In Product Advertising API, the shopping cart is considered remote
        because it is hosted by Amazon servers. In this way, the cart is remote
        to the vendor's web site where the customer views and selects the items
        they want to purchase.

        Once you add an item to a cart by specifying the item's ListItemId and
        ASIN, or OfferListing ID, the item is assigned a ``CartItemId`` and
        accessible only by that value. That is, in subsequent requests, an item
        in a cart cannot be accessed by its ``ListItemId`` and ``ASIN``, or
        ``OfferListingId``. ``CartItemId`` is returned by ``CartCreate``,
        ``CartGet``, and ``CartAdd``.

        Because the contents of a cart can change for different reasons, such
        as item availability, you should not keep a copy of a cart locally.
        Instead, use the other cart operations to modify the cart contents. For
        example, to retrieve contents of the cart, which are represented by
        CartItemIds, use ``CartGet``.

        Available products are added as cart items. Unavailable items, for
        example, items out of stock, discontinued, or future releases, are
        added as SaveForLaterItems. No error is generated. The Amazon database
        changes regularly. You may find a product with an offer listing ID but
        by the time the item is added to the cart the product is no longer
        available. The checkout page in the Order Pipeline clearly lists items
        that are available and those that are SaveForLaterItems.

        It is impossible to create an empty shopping cart. You have to add at
        least one item to a shopping cart using a single ``CartCreate``
        request.  You can add specific quantities (up to 999) of each item.

        ``CartCreate`` can be used only once in the life cycle of a cart. To
        modify the contents of the cart, use one of the other cart operations.

        Carts cannot be deleted. They expire automatically after being unused
        for 7 days. The lifespan of a cart restarts, however, every time a cart
        is modified. In this way, a cart can last for more than 7 days. If, for
        example, on day 6, the customer modifies a cart, the 7 day countdown
        starts over.
        """
        try:
            params.update(self._convert_cart_items(items))
            return self.call(Operation='CartCreate', **params)
        except AWSError, e:

            if e.code == 'AWS.MissingParameters':
                raise ValueError(e.msg)

            if e.code == 'AWS.ParameterOutOfRange':
                raise ValueError(e.msg)

            if e.code == 'AWS.ECommerceService.ItemNotEligibleForCart':
                raise InvalidCartItem(e.msg)

            # otherwise re-raise exception
            raise # pragma: no cover

    def cart_add(self, cart_id, hmac, items, **params):
        """
        The ``CartAdd`` operation enables you to add items to an existing
        remote shopping cart. ``CartAdd`` can only be used to place a new item
        in a shopping cart. It cannot be used to increase the quantity of an
        item already in the cart. If you would like to increase the quantity of
        an item that is already in the cart, you must use the ``CartModify``
        operation.

        You add an item to a cart by specifying the item's ``OfferListingId``,
        or ``ASIN`` and ``ListItemId``. Once in a cart, an item can only be
        identified by its ``CartItemId``. That is, an item in a cart cannot be
        accessed by its ASIN or OfferListingId. CartItemId is returned by
        ``CartCreate``, ``CartGet``, and ``CartAdd``.

        To add items to a cart, you must specify the cart using the ``CartId``
        and ``HMAC`` values, which are returned by the ``CartCreate``
        operation.

        If the associated CartCreate request specified an AssociateTag, all
        ``CartAdd`` requests must also include a value for Associate Tag
        otherwise the request will fail.

        .. note:: Some manufacturers have a minimum advertised price (MAP) that
           can be displayed on Amazon's retail web site. In these cases, when
           performing a Cart operation, the MAP Is returned instead of the
           actual price. The only way to see the actual price is to add the
           item to a remote shopping cart and follow the PurchaseURL. The
           actual price will be the MAP or lower.
        """
        try:
            params.update({
                'CartId' : cart_id,
                'HMAC' : hmac,
            })
            params.update(self._convert_cart_items(items))
            return self.call(Operation='CartAdd', **params)
        except AWSError, e:

            if e.code == 'AWS.ECommerceService.InvalidCartId':
                raise InvalidCartId

            if e.code == 'AWS.ECommerceService.CartInfoMismatch':
                raise CartInfoMismatch

            if e.code == 'AWS.MissingParameters':
                raise ValueError(e.msg)

            if e.code == 'AWS.ParameterOutOfRange':
                raise ValueError(e.msg)

            if e.code == 'AWS.ECommerceService.ItemNotEligibleForCart':
                raise InvalidCartItem(e.msg)

            if e.code == 'AWS.ECommerceService.ItemAlreadyInCart':
                item = self._reg('already-in-cart').search(e.msg).group('item')
                raise ItemAlreadyInCart(item)

            # otherwise re-raise exception
            raise # pragma: no cover

    def cart_modify(self, cart_id, hmac, item_ids, **params):
        """
        The ``CartModify`` operation enables you to change the quantity of
        items that are already in a remote shopping cart and move items from
        the active area of a cart to the SaveForLater area or the reverse.

        To modify the number of items in a cart, you must specify the cart
        using the CartId and HMAC values that are returned in the CartCreate
        operation. A value similar to HMAC, URLEncodedHMAC, is also returned.
        This value is the URL encoded version of the HMAC. This encoding is
        necessary because some characters, such as + and /, cannot be included
        in a URL. Rather than encoding the HMAC yourself, use the
        URLEncodedHMAC value for the HMAC parameter.

        You can use ``CartModify`` to modify the number of items in a remote
        shopping cart by setting the value of the Quantity parameter
        appropriately. You can eliminate an item from a cart by setting the
        value of the Quantity parameter to zero. Or, you can double the number
        of a particular item in the cart by doubling its Quantity . You cannot,
        however, use ``CartModify`` to add new items to a cart.

        If the associated CartCreate request specified an AssociateTag, all
        ``CartModify`` requests must also include a value for Associate Tag
        otherwise the request will fail.
        """
        # TODO Action=SaveForLater
        try:
            params.update({
                'CartId' : cart_id,
                'HMAC' : hmac,
            })
            params.update(self._convert_cart_items(item_ids, key='CartItemId'))
            return self.call(Operation='CartModify', **params)
        except AWSError, e:

            if e.code == 'AWS.ECommerceService.CartInfoMismatch':
                raise CartInfoMismatch

            if e.code == 'AWS.MissingParameters':
                raise ValueError(e.msg)

            if e.code == 'AWS.ParameterOutOfRange':
                raise ValueError(e.msg)

            if e.code == 'AWS.ECommerceService.ItemNotEligibleForCart':
                raise InvalidCartItem(e.msg)

            # otherwise re-raise exception
            raise # pragma: no cover

    def cart_get(self, cart_id, hmac, **params):
        """
        The ``CartGet`` operation enables you to retrieve the IDs, quantities,
        and prices of all of the items, including SavedForLater items in a
        remote shopping cart.

        Because the contents of a cart can change for different reasons, such
        as availability, you should not keep a copy of a cart locally. Instead,
        use ``CartGet`` to retrieve the items in a remote shopping cart.

        To retrieve the items in a cart, you must specify the cart using the
        ``CartId`` and ``HMAC`` values, which are returned in the
        ``CartCreate`` operation.  A value similar to HMAC, ``URLEncodedHMAC``,
        is also returned. This value is the URL encoded version of the
        ``HMAC``. This encoding is necessary because some characters, such as
        ``+`` and ``/``, cannot be included in a URL.  Rather than encoding the
        ``HMAC`` yourself, use the ``URLEncodedHMAC`` value for the HMAC
        parameter.

        ``CartGet`` does not work after the customer has used the
        ``PurchaseURL`` to either purchase the items or merge them with the
        items in their Amazon cart.

        If the associated ``CartCreate`` request specified an ``AssociateTag``,
        all ``CartGet`` requests must also include a value for ``AssociateTag``
        otherwise the request will fail.
        """
        try:
            params.update({
                'CartId' : cart_id,
                'HMAC' : hmac,
            })
            return self.call(Operation='CartGet', **params)
        except AWSError, e:

            if e.code == 'AWS.ECommerceService.CartInfoMismatch':
                raise CartInfoMismatch

            # otherwise re-raise exception
            raise # pragma: no cover

    def cart_clear(self, cart_id, hmac, **params):
        """
        The ``CartClear`` operation enables you to remove all of the items in a
        remote shopping cart, including SavedForLater items. To remove only
        some of the items in a cart or to reduce the quantity of one or more
        items, use ``CartModify``.

        To delete all of the items from a remote shopping cart, you must
        specify the cart using the ``CartId`` and ``HMAC`` values, which are
        returned by the ``CartCreate`` operation. A value similar to the
        ``HMAC``, ``URLEncodedHMAC``, is also returned. This value is the URL
        encoded version of the ``HMAC``. This encoding is necessary because
        some characters, such as ``+`` and ``/``, cannot be included in a URL.
        Rather than encoding the ``HMAC`` yourself, use the U``RLEncodedHMAC``
        value for the HMAC parameter.

        ``CartClear`` does not work after the customer has used the
        ``PurchaseURL`` to either purchase the items or merge them with the
        items in their Amazon cart.

        Carts exist even though they have been emptied. The lifespan of a cart
        is 7 days since the last time it was acted upon. For example, if a cart
        created 6 days ago is modified, the cart lifespan is reset to 7 days.
        """
        try:
            params.update({
                'CartId' : cart_id,
                'HMAC' : hmac,
            })
            return self.call(Operation='CartClear', **params)
        except AWSError, e:

            if e.code == 'AWS.ECommerceService.CartInfoMismatch':
                raise CartInfoMismatch

            # otherwise re-raise exception
            raise # pragma: no cover

    def deprecated_operation(self, *args, **kwargs):
        """
        Some operations are deprecated and will be answered with HTTP 410. To
        avoid unnecessary API calls, a ``DeprecatedOperation`` exception is
        thrown straight-away.
        """
        raise DeprecatedOperation

    # shortcuts for deprecated operations
    customer_content_lookup = customer_content_search = deprecated_operation
    help = deprecated_operation
    list_lookup = list_search = deprecated_operation
    tag_lookup = deprecated_operation
    transaction_lookup = deprecated_operation
    vehicle_part_lookup = vehicle_part_search = deprecated_operation
    vehicle_search = deprecated_operation

    # deprecated since 2011-08-01
    seller_lookup = deprecated_operation
    seller_listing_lookup = seller_listing_search = deprecated_operation

    #: MultiOperation is supported outside this API
    multi_operation = None

