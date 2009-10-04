
"""
Find similar items to "Small Favor: A Novel of the Dresden Files"
(ASIN 0451462009).
"""

from config import AWS_KEY, SECRET_KEY
from amazon.product import API
from amazon.product import ResultPaginator

if __name__ == '__main__':
    
    api = API(AWS_KEY, SECRET_KEY)
    root = api.similarity_lookup('0451462009')
    
    #~ from lxml import etree
    #~ print etree.tostring(root, pretty_print=True)
    
    nspace = root.nsmap.get(None, '')
    books = root.xpath('//aws:Items/aws:Item', 
                         namespaces={'aws' : nspace})
    
    for book in books:
        print 'ASIN %-10s' % book.ASIN,
        print unicode(book.ItemAttributes.Author), ':', 
        print unicode(book.ItemAttributes.Title)
        