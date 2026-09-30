"""
DynamoDB simulado (varias tablas) para probar el Lambda sin AWS ni boto3.

No es el DynamoDB real, pero imita a proposito las reglas que mas suelen
romper codigo real:
  * put/update rechazan float (boto3 exige Decimal); los enteros se guardan y se
    leen como Decimal, igual que en AWS (por eso json.dumps de un item crudo falla)
  * placeholders (#x / :x) sin usar en las expresiones -> error; usados sin definir -> error
  * UpdateExpression: SET (con if_not_exists), ADD, REMOVE
  * ConditionExpression en texto (attribute_exists / attribute_not_exists / < / =, con AND / OR)
    y como objetos Attr(...).lt() / .not_exists() combinados con |
  * update_item hace "upsert" (crea el item si no existe) salvo que la condicion falle
  * query sobre indices (GSI) dispersos: los items sin la clave del indice no aparecen
  * query con Limit, ScanIndexForward y paginacion (LastEvaluatedKey)
"""
import copy
import re
import sys
import types
from decimal import Decimal


class ClientError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"{code}: {msg}")
        self.response = {"Error": {"Code": code, "Message": msg}}


class ConditionalCheckFailedException(ClientError):
    def __init__(self, msg="The conditional request failed"):
        super().__init__("ConditionalCheckFailedException", msg)


# ---------------------------------------------------------------- condiciones (objetos)
class _Cond:
    def __init__(self, fn):
        self.fn = fn

    def __or__(self, other):
        return _Cond(lambda item: self.fn(item) or other.fn(item))

    def __and__(self, other):
        return _Cond(lambda item: self.fn(item) and other.fn(item))


class _KeyEq:
    def __init__(self, attr, value):
        self.attr, self.value = attr, value


class Key:
    def __init__(self, attr):
        self.attr = attr

    def eq(self, value):
        return _KeyEq(self.attr, value)


class Attr:
    def __init__(self, attr):
        self.attr = attr

    def lt(self, v):
        return _Cond(lambda item: item is not None and self.attr in item and item[self.attr] < v)

    def not_exists(self):
        return _Cond(lambda item: item is None or self.attr not in item)

    def exists(self):
        return _Cond(lambda item: item is not None and self.attr in item)


# ------------------------------------------------------------------------- utilidades
def _norm(x, path="Item"):
    """Valida (sin floats) y convierte int -> Decimal, como hace DynamoDB."""
    if isinstance(x, float):
        raise TypeError(f"Float types are not supported. Use Decimal types instead. ({path})")
    if isinstance(x, bool):
        return x
    if isinstance(x, int):
        return Decimal(x)
    if isinstance(x, dict):
        return {k: _norm(v, f"{path}.{k}") for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_norm(v, f"{path}[{i}]") for i, v in enumerate(x)]
    return x


_TOKEN = re.compile(r"[#:][A-Za-z0-9_]+")

# Palabras reservadas de DynamoDB (subconjunto): usarlas sin alias (#x) en una
# expresion es un error real en AWS que solo aparece en produccion.
RESERVED = {"NAME", "STATUS", "DAY", "DATE", "TIME", "TIMESTAMP", "USER", "SOURCE", "ZONE", "DATA", "HASH",
            "KEY", "VALUE", "VALUES", "ORDER", "GROUP", "COUNT", "LOCATION", "TYPE", "YEAR", "MONTH", "STATE",
            "TEXT", "ROLE", "TOTAL", "ITEMS", "SET", "ADD", "REMOVE", "BY", "TO", "IN", "IS", "ON", "OR", "AND", "NOT"}


def _check_reserved(name):
    if name.upper() in RESERVED:
        raise ClientError("ValidationException", f"Attribute name is a reserved keyword; reserved keyword: {name}")
    return name


def _split_top(s, sep=","):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [c.strip() for c in out]


class FakeTable:
    def __init__(self, name, hash_key, gsis):
        self.name, self.hash_key, self.gsis = name, hash_key, gsis  # gsis: {index: (hash, range)}
        self.items = {}

    def _k(self, key):
        return key[self.hash_key]

    def put_item(self, Item):
        self.items[self._k(Item)] = copy.deepcopy(_norm(Item))
        return {}

    def get_item(self, Key):
        it = self.items.get(self._k(Key))
        return {"Item": copy.deepcopy(it)} if it is not None else {}

    # ---- query sobre un indice
    def query(self, IndexName, KeyConditionExpression, ScanIndexForward=True, Limit=None, ExclusiveStartKey=None):
        if IndexName not in self.gsis:
            raise ClientError("ValidationException", f"The table does not have the specified index: {IndexName}")
        hk, rk = self.gsis[IndexName]
        assert isinstance(KeyConditionExpression, _KeyEq), "solo Key(...).eq(...)"
        if KeyConditionExpression.attr != hk:
            raise ClientError("ValidationException", f"Query condition missed key schema element: {hk}")
        rows = [copy.deepcopy(i) for i in self.items.values()
                if hk in i and rk in i and i[hk] == KeyConditionExpression.value]
        rows.sort(key=lambda r: (r[rk], r[self.hash_key]), reverse=not ScanIndexForward)
        start = 0
        if ExclusiveStartKey:
            ids = [r[self.hash_key] for r in rows]
            start = ids.index(ExclusiveStartKey[self.hash_key]) + 1
        rows = rows[start:]
        out = {"Items": rows}
        if Limit and len(rows) > Limit:
            out["Items"] = rows[:Limit]
            out["LastEvaluatedKey"] = {self.hash_key: rows[Limit - 1][self.hash_key]}
        return out

    # ---- condiciones
    def _eval_str(self, expr, item, names, values):
        def val(tok):
            return values[tok]

        def attr(name):
            return names[name] if name.startswith("#") else _check_reserved(name)

        for or_part in re.split(r"\s+OR\s+", expr):
            ok = True
            for clause in re.split(r"\s+AND\s+", or_part):
                clause = clause.strip()
                m = re.fullmatch(r"attribute_exists\(([#\w]+)\)", clause)
                if m:
                    ok = ok and item is not None and attr(m.group(1)) in item
                    continue
                m = re.fullmatch(r"attribute_not_exists\(([#\w]+)\)", clause)
                if m:
                    ok = ok and (item is None or attr(m.group(1)) not in item)
                    continue
                m = re.fullmatch(r"([#\w]+)\s*(<|=)\s*(:\w+)", clause)
                if not m:
                    raise ClientError("ValidationException", f"condicion no soportada: {clause}")
                a, op, v = attr(m.group(1)), m.group(2), val(m.group(3))
                cur = item.get(a) if item is not None else None
                if cur is None:
                    ok = False
                elif op == "<":
                    ok = ok and cur < v
                else:
                    ok = ok and cur == v
            if ok:
                return True
        return False

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None, ReturnValues=None):
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        _norm(values, "values")

        cond_str = ConditionExpression if isinstance(ConditionExpression, str) else ""
        used = set(_TOKEN.findall(UpdateExpression)) | set(_TOKEN.findall(cond_str))
        for tok in used:
            if tok.startswith("#") and tok not in names:
                raise ClientError("ValidationException", f"undefined attribute name {tok}")
            if tok.startswith(":") and tok not in values:
                raise ClientError("ValidationException", f"undefined attribute value {tok}")
        for tok in list(names) + list(values):
            if tok not in used:
                raise ClientError("ValidationException", f"unused placeholder {tok}")
        values = {k: _norm(v) for k, v in values.items()}

        key = self._k(Key)
        item = self.items.get(key)

        if ConditionExpression is not None:
            if isinstance(ConditionExpression, _Cond):
                ok = ConditionExpression.fn(item)
            else:
                ok = self._eval_str(ConditionExpression, item, names, values)
            if not ok:
                raise ConditionalCheckFailedException()

        new = copy.deepcopy(item) if item is not None else {self.hash_key: Key[self.hash_key]}
        updated = {}

        parts = re.split(r"\b(SET|ADD|REMOVE)\b", UpdateExpression)
        clauses = {}
        for i in range(1, len(parts), 2):
            if parts[i] in clauses:
                raise ClientError("ValidationException", "clausula repetida")
            clauses[parts[i]] = parts[i + 1].strip()
        if parts[0].strip() or not clauses:
            raise ClientError("ValidationException", f"UpdateExpression invalida: {UpdateExpression}")

        def real(name):
            return names[name] if name.startswith("#") else _check_reserved(name)

        for assign in _split_top(clauses.get("SET", "")):
            m = re.fullmatch(r"([#\w]+)\s*=\s*(.+)", assign)
            if not m:
                raise ClientError("ValidationException", f"asignacion invalida: {assign}")
            target, rhs = real(m.group(1)), m.group(2).strip()
            mm = re.fullmatch(r"if_not_exists\(([#\w]+),\s*(:\w+)\)", rhs)
            if mm:
                a = real(mm.group(1))
                newv = new[a] if a in new else values[mm.group(2)]
            elif re.fullmatch(r":\w+", rhs):
                newv = values[rhs]
            else:
                raise ClientError("ValidationException", f"expresion no soportada: {rhs}")
            new[target] = copy.deepcopy(newv)
            updated[target] = new[target]
        for add in _split_top(clauses.get("ADD", "")):
            m = re.fullmatch(r"([#\w]+)\s+(:\w+)", add)
            if not m:
                raise ClientError("ValidationException", f"ADD invalido: {add}")
            a, v = real(m.group(1)), values[m.group(2)]
            new[a] = new.get(a, Decimal(0)) + v
            updated[a] = new[a]
        for rem in _split_top(clauses.get("REMOVE", "")):
            if not re.fullmatch(r"[#\w]+", rem):
                raise ClientError("ValidationException", f"REMOVE invalido: {rem}")
            new.pop(real(rem), None)

        self.items[key] = new
        out = {}
        if ReturnValues == "UPDATED_NEW":
            out["Attributes"] = copy.deepcopy(updated)
        return out


TABLES = {}


def make_tables(orders="orders", coupons="coupons", customers="customers", geo="geo"):
    TABLES.clear()
    TABLES[orders] = FakeTable(orders, "order_id", {"phone-index": ("phone", "created_at"),
                                                     "day-index": ("delivery_day", "created_at")})
    TABLES[coupons] = FakeTable(coupons, "code", {})
    TABLES[customers] = FakeTable(customers, "phone", {})
    TABLES[geo] = FakeTable(geo, "pk", {})


def install():
    """Inyecta modulos falsos boto3/botocore antes de importar handler.py."""
    boto3 = types.ModuleType("boto3")

    class _Exc:
        ConditionalCheckFailedException = ConditionalCheckFailedException

    class _Client:
        exceptions = _Exc

    class _Meta:
        client = _Client

    class _Resource:
        meta = _Meta

        def Table(self, name):
            return TABLES[name]

    boto3.resource = lambda *_a, **_k: _Resource()
    dynamodb = types.ModuleType("boto3.dynamodb")
    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Key = Key
    conditions.Attr = Attr
    sys.modules.update({"boto3": boto3, "boto3.dynamodb": dynamodb, "boto3.dynamodb.conditions": conditions})


def reset():
    for t in TABLES.values():
        t.items.clear()
