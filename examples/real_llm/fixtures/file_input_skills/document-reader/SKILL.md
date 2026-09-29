---
name: document-reader
description: 阅读用户附带的文档，登记其中印着的核对码
version: 1.0.0
type: composite
entry: true
tool_names: [record_document_code]
max_call_depth: 1
---

# 文档阅读器

当用户消息附带 PDF 文档时，读取文档里印着的核对码（逐字，不猜测、不补全），
然后必须调用一次 `record_document_code` 登记该核对码。收到工具成功结果后，
用一句话复述已登记的核对码。文档里没有核对码时如实说明，不要编造。
