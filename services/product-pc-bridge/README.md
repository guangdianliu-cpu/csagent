# 产品中心只读 HTTP 网关

```
cs-agent → HTTP :18080 → product-pc-bridge → Dubbo Facade（产品中心）
```

## 包结构

```
src/main/java/.../pcbridge/
├── ProductPcBridgeApplication.java
├── config/DubboConsumerConfiguration.java   # @ImportResource dubbo-consumer.xml
├── mapper/ProductFieldMapper.java
├── service/
│   ├── ProductQueryService.java
│   └── DubboProductQueryService.java        # 构造注入 XML 里的 Facade
└── web/FundController.java

src/main/resources/
├── application.yml
└── dubbo/dubbo-consumer.xml                 # 对齐 fdps consumer（只读）
```

## 启动

**必须用 JDK 8 运行**（pom `java.version=1.8`，且 `com.alibaba:dubbo:2.6.7` 的
adaptive 扩展在 JDK 9+ 模块化 JVM 下编译会失败，报
`ExceptionInInitializerError` / `Can't create adaptive extension ... Protocol$Adaptive`）。
IDEA 里请把该模块的 Project SDK / Run Configuration 的 JRE 设为本机 JDK 8
（如 `D:\software\jdk1.8.0_251`），不要用 IDEA 默认的 17/21。

```bash
cd services/product-pc-bridge
set ZOOKEEPER_URL=zookeeper://10.1.1.139:2181
set DUBBO_ENV=test
set DUBBO_VERSION=1.0.0
mvn -q clean spring-boot:run
curl http://127.0.0.1:18080/api/health
curl http://127.0.0.1:18080/api/funds/000001
# 额度查询必填网点号；默认 0001（可用 ?net_no=xxxx 覆盖）
curl "http://127.0.0.1:18080/api/quota/000001"
curl "http://127.0.0.1:18080/api/quota/000001?net_no=0001"
```

已用 JDK 8 + 测试 ZK（10.1.1.139:2181）验证：Facade bean 创建成功、
`/api/funds/000001` 返回真实产品中心数据（华夏成长混合）。

## Maven 依赖要点

```xml
<!-- groupId 必须是 com.leadbank.productcenter.facade（不是 com.leadbank.productcenter） -->
com.leadbank.productcenter.facade:leadbank-productcenter-facade:0.0.2-35629-SNAPSHOT
<!-- 必须显式加，否则缺 ICommonRequest → fundBaseInfoFacade ExceptionInInitializerError -->
com.leadbank.common:leadbank-biz-common:202410.1-SNAPSHOT
com.leadbank.common:leadbank-rpc-common:202410.1-SNAPSHOT
```

私服：`http://10.1.1.135:9090/nexus/content/repositories/snapshots/`

若仍报 `fundBaseInfoFacade` / `ExceptionInInitializerError`，请把日志里 **Caused by** 整段贴出。

## Facade 对照

见仓库内 HTTP ↔ Facade 表；额度只读，禁止 freeze/deduct。

## 批量查询（fund_code in）

多只基金一次取费率/限额/折扣（逗号分隔，最多 10 只）：

```bash
curl "http://127.0.0.1:18080/api/funds/batch/fees?codes=023634,022040"
curl "http://127.0.0.1:18080/api/funds/batch/trade-limits?codes=023634,022040"
curl "http://127.0.0.1:18080/api/funds/batch/discounts?codes=023634,022040"
```

单码接口 `/api/funds/{code}/fees` 等仍保留。
