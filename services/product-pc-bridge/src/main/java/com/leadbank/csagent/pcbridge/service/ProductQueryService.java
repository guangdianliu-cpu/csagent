package com.leadbank.csagent.pcbridge.service;

import java.util.List;
import java.util.Map;

public interface ProductQueryService {
    Map<String, Object> health();

    /**
     * 对齐 fims detail.app HEADER。
     * <p>默认机构口径（custtype=0）；个人请显式传 custtype=1。
     */
    Map<String, Object> fundDetail(String fundCode);

    /**
     * 按客户类型查详情（0 机构 / 1 个人）。
     * 并行拉 CMS + Retail 确认日 + 费率；单通道 trade_by_custtype，避免双查冗余。
     */
    Map<String, Object> fundDetail(String fundCode, String custType);

    /** 档案 + 经理列表：FundInfoFacade.querFundArchiveInfo */
    Map<String, Object> fundArchive(String fundCode);

    /** 风险指标：FundInfoExtendFacade.queryFundRiskIndex */
    Map<String, Object> fundRiskIndex(String fundCode);

    /** 重仓持仓：FundInfoFacade.queryFundAssetPosition */
    Map<String, Object> fundHoldings(String fundCode);

    /** 历史规模：FundInfoExtendFacade.queryFundAssetScaleList */
    List<Map<String, Object>> fundAssetScale(String fundCode);

    /** 持有人结构：FundInfoExtendFacade.queryFundHolderInfoList */
    List<Map<String, Object>> fundHolders(String fundCode);

    /** 分红记录：FundInfoFacade.queryDividendDistrByFundCode（detail.app 关联） */
    List<Map<String, Object>> fundDividends(String fundCode);

    /** 投资风格：FundInfoExtendFacade.queryFundInvestStyle */
    Map<String, Object> fundInvestStyle(String fundCode);

    List<Map<String, Object>> feeRates(String fundCode);

    List<Map<String, Object>> tradeLimits(String fundCode);

    List<Map<String, Object>> discounts(String fundCode);

    /** 多基金代码批量查费率（codes 逗号分隔或列表；单码失败不拖垮整批） */
    List<Map<String, Object>> feeRatesBatch(List<String> fundCodes);

    List<Map<String, Object>> tradeLimitsBatch(List<String> fundCodes);

    List<Map<String, Object>> discountsBatch(List<String> fundCodes);

    /**
     * 开放期查询。
     * @param businessCode 业务代码 020认购/022申购/024赎回；空则查全部三类
     */
    List<Map<String, Object>> openPeriods(String fundCode, String businessCode);

    Map<String, Object> performance(String fundCode);

    /**
     * 涨幅排行。
     * @param fundType 01股票/02债/03混合/04货币；空则合并查四类后截断
     */
    List<Map<String, Object>> ranking(String orderBy, String direction, int limit, String fundType);

    /** @param netNo 网点号，可空（用 bridge.default-net-no） */
    Map<String, Object> quotaInfo(String fundCode, String netNo);

    /** @param netNo 网点号，可空（用 bridge.default-net-no） */
    List<Map<String, Object>> quotaControls(String fundCode, String netNo);

    Map<String, Object> currentWorkday();

    /** 区间工作日列表：WorkdayFacade.queryWorkdayList */
    List<Map<String, Object>> workdayList(String startDate, String endDate);

    /** 判断某日是否工作日：WorkdayFacade.checkIsBusinessDay */
    Map<String, Object> checkWorkday(String date);

    Map<String, Object> taInfo(String taCode);

    /** TA 名称/代码模糊：TaInfoFacade.queryAllTaInfo(taName/taCodeOrName) */
    List<Map<String, Object>> searchTa(String keyword, int limit);

    Map<String, Object> managerOrg(String managerCode);

    /** 管理人名称/代码模糊：ProductManageFacade.queryFundManagerList */
    List<Map<String, Object>> searchManagers(String keyword, int limit);

    /** 基金名称/代码模糊：FundBaseInfoFacade.fundBaseInfoPage；可附带管理人/TA/代销等筛选 */
    List<Map<String, Object>> searchFunds(
            String keyword,
            int limit,
            String managerCode,
            String taCode,
            String consignFlag,
            String fundType,
            String fundState,
            String productType);

    /** 按主基金代码查同系列从基金份额：OnlineFacade.queryFundByMainFund */
    List<Map<String, Object>> listByMainFundCode(String mainFundCode);
}
