"""A1 接口只接收已登记根编号，不接受浏览器提供的磁盘路径。"""

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    root_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    verify_content: StrictBool = True
