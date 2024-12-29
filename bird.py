from typing import List
from typing import Any
from dataclasses import dataclass
import json
@dataclass
class Bird:
    images: List[str]
    lengthMin: str
    lengthMax: str
    name: str
    wingspanMin: str
    id: int
    wingspanMax: str
    sciName: str
    region: List[str]
    family: str
    order: str
    status: str
    
    @staticmethod
    def from_dict(obj: Any) -> 'Bird':
        _images = [str(y) for y in obj.get("images")]
        _lengthMin = str(obj.get("lengthMin"))
        _lengthMax = str(obj.get("lengthMax"))
        _name = str(obj.get("name"))
        _wingspanMin = str(obj.get("wingspanMin"))
        _id = int(obj.get("id"))
        _wingspanMax = str(obj.get("wingspanMax"))
        _sciName = str(obj.get("sciName"))
        _region = [str(y) for y in obj.get("region")]
        _family = str(obj.get("family"))
        _order = str(obj.get("order"))
        _status = str(obj.get("status"))
        return Bird(_images, _lengthMin, _lengthMax, _name, _wingspanMin, _id, _wingspanMax, _sciName, _region, _family, _order, _status)

@dataclass
class Response:
    entities: List[Bird]
    total: int
    page: int
    pageSize: int

    @staticmethod
    def from_dict(obj: Any) -> 'Response':
        _entities = [Bird.from_dict(y) for y in obj.get("entities")]
        _total = int(obj.get("total"))
        _page = int(obj.get("page"))
        _pageSize = int(obj.get("pageSize"))
        return Response(_entities, _total, _page, _pageSize)

# Example Usage
# jsonstring = json.loads(myjsonstring)
# root = Root.from_dict(jsonstring)

