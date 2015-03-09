import urllib2
import json


instanceTypes = []

urlsToAmazonInstanceTypes = ['http://a0.awsstatic.com/pricing/1/ec2/linux-od.min.js','http://a0.awsstatic.com/pricing/1/ec2/previous-generation/linux-od.min.js']

def reverseReplace(s, old, new, occurrence):
    li = s.rsplit(old, occurrence)
    return new.join(li)

def convertKeysToJSON(javaScriptString):
    javaScriptString = javaScriptString.replace('vers:0.01,','')
    javaScriptString = javaScriptString.replace('config','"config"')
    javaScriptString = javaScriptString.replace('rate:','"rate":')
    javaScriptString = javaScriptString.replace('valueColumns:','"valueColumns":')
    javaScriptString = javaScriptString.replace('name:','"name":')
    javaScriptString = javaScriptString.replace('prices:','"prices":')
    javaScriptString = javaScriptString.replace('USD:','"USD":')
    javaScriptString = javaScriptString.replace('currencies:','"currencies":')
    javaScriptString = javaScriptString.replace('regions:','"regions":')
    javaScriptString = javaScriptString.replace('region:','"region":')
    javaScriptString = javaScriptString.replace('instanceTypes:','"instanceTypes":')
    javaScriptString = javaScriptString.replace('type:','"type":')
    javaScriptString = javaScriptString.replace('sizes:','"sizes":')
    javaScriptString = javaScriptString.replace('size:','"size":')
    javaScriptString = javaScriptString.replace('vCPU:','"vCPU":')
    javaScriptString = javaScriptString.replace('ECU:','"ECU":')
    javaScriptString = javaScriptString.replace('memoryGiB:','"memoryGiB":')
    javaScriptString = javaScriptString.replace('storageGB:','"storageGB":')

    return javaScriptString

def responseToString(respString):
    return str(respString)

def getInstanceTypes():
    for url in urlsToAmazonInstanceTypes:
        req = urllib2.Request(url)
        response = urllib2.urlopen(req)
        responsePage = response.read()
        response.close()

        responseString = responseToString(responsePage)

        #remove the string 'callback(' and anything preceding it
        throwAwayString, javaScriptString = responseString.split("callback(",1)

        #remove the last string ');' and anything after it
        javaScriptString = reverseReplace(javaScriptString,');','',1)
        javaScriptString = convertKeysToJSON(javaScriptString)

        #load the string as a json object
        dictionaryObject=json.loads(javaScriptString)
        configDict = dictionaryObject['config']
        regions = configDict['regions']
        for i in regions:
            instanceTypesDict =  i['instanceTypes']
            for j in instanceTypesDict:
                for w in j['sizes']:
                    #collect the size the CPU and the memory
                    instanceTypes.append((w['size'],w['vCPU'],w['memoryGiB']))

#getInstanceTypes()
#print instanceTypes
