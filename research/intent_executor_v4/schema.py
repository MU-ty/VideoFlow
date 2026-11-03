"""Same structured-output transport for all arms; no gold-dependent schema."""

def obj(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


def response_format(arm,initial):
    values={'color':{'type':'string','enum':['white','black','blue','red']},
            'light':{'type':'string','enum':['day','sunset']},'subtitle':{'type':'string'}}
    if arm=='state': schema=obj({'state':obj({s:obj(values) for s in initial})})
    else:
        options=[]
        for field,value_schema in values.items():
            common={'shot':{'type':'string','enum':list(initial)},'field':{'type':'string','enum':[field]}}
            options.append(obj({'op':{'type':'string','enum':['set']},**common,'value':value_schema}))
            if arm=='program':
                options.append(obj({'op':{'type':'string','enum':['restore_before']},**common,'turn':{'type':'integer','minimum':1}}))
                options.append(obj({'op':{'type':'string','enum':['copy_current']},**common,'source':{'type':'string','enum':list(initial)}}))
        schema=obj({'ops':{'type':'array','items':{'anyOf':options},'maxItems':30}})
    return {'type':'json_schema','json_schema':{'name':'edit_'+arm,'strict':True,'schema':schema}}


def validate(value,schema):
    """Validate only the JSON Schema subset produced above; fail closed."""
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try: validate(value,option); return
            except ValueError: pass
        raise ValueError('no schema alternative matched')
    typ=schema['type']
    if typ=='object':
        if not isinstance(value,dict) or set(value)!=set(schema['properties']): raise ValueError('object fields')
        for k,v in value.items(): validate(v,schema['properties'][k])
    elif typ=='array':
        if not isinstance(value,list) or len(value)>schema.get('maxItems',100000): raise ValueError('array')
        for item in value: validate(item,schema['items'])
    elif typ=='string':
        if not isinstance(value,str): raise ValueError('string')
    elif typ=='integer':
        if type(value)!=int or value<schema.get('minimum',value): raise ValueError('integer')
    else: raise ValueError('unsupported schema type')
    if 'enum' in schema and value not in schema['enum']: raise ValueError('enum')
