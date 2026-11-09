package com.leadbank.csagent.pcbridge.service;

import com.leadbank.csagent.pcbridge.mapper.ProductFieldMapper;
import com.leadbank.productcenter.facade.abs.FundOnlineAbsResponse;
import com.leadbank.productcenter.facade.bean.cms.*;
import com.leadbank.productcenter.facade.bean.cms.FundDividendResp;
import com.leadbank.productcenter.facade.bean.manager.query.QueryFundManagerNameReq;
import com.leadbank.productcenter.facade.bean.manager.query.QueryFundManagerNameResp;
import com.leadbank.productcenter.facade.bean.product.*;
import com.leadbank.productcenter.facade.cms.FundInfoFacade;
import com.leadbank.productcenter.facade.cms.FundStatisticsFacade;
import com.leadbank.productcenter.facade.cms.OnlineFacade;
import com.leadbank.productcenter.facade.param.TaInfoFacade;
import com.leadbank.productcenter.facade.param.WorkdayFacade;
import com.leadbank.productcenter.facade.param.req.CheckIsBusinessDayReq;
import com.leadbank.productcenter.facade.param.req.QueryCurrentBusinessDateReq;
import com.leadbank.productcenter.facade.param.req.TaInfoReq;
import com.leadbank.productcenter.facade.param.req.WorkdayDifferenceReq;
import com.leadbank.productcenter.facade.param.resp.CheckIsBusinessDayResp;
import com.leadbank.productcenter.facade.param.resp.QueryCurrentBusinessDateResp;
import com.leadbank.productcenter.facade.param.resp.QueryWorkdayListResp;
import com.leadbank.productcenter.facade.param.resp.TaInfo;
import com.leadbank.productcenter.facade.param.resp.TaInfoResp;
import com.leadbank.productcenter.facade.product.FundBaseInfoFacade;
import com.leadbank.productcenter.facade.product.FundInfoExtendFacade;
import com.leadbank.productcenter.facade.product.FundManagerFacade;
import com.leadbank.productcenter.facade.product.FundTrnFacade;
import com.leadbank.productcenter.facade.product.ProductManageFacade;
import com.leadbank.productcenter.facade.quota.ProductQuotaFacade;
import com.leadbank.productcenter.facade.quota.QueryQuotaConfigResp;
import com.leadbank.productcenter.facade.quota.QueryQuotaRequest;
import com.leadbank.productcenter.facade.quota.QueryQuotaResp;
import com.leadbank.productcenter.facade.bean.retail.FundInfoRetailBean;
import com.leadbank.productcenter.facade.bean.retail.FundInfoRetailReq;
import com.leadbank.productcenter.facade.bean.retail.FundInfoRetailResp;
import com.leadbank.productcenter.facade.retail.FundRetailFacade;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.util.*;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * Dubbo 适配：对齐 fims detail.app 产品中心只读调用。
 */
@Service
public class DubboProductQueryService implements ProductQueryService {

    private static final Logger log = LoggerFactory.getLogger(DubboProductQueryService.class);

    @Value("${bridge.sys-id:CSAGENT}")
    private String sysId;

    @Value("${bridge.api-version:1.0}")
    private String apiVersion;

    /** 对齐 fims CommConstant.NET_NO；额度接口强校验网点号 */
    @Value("${bridge.default-net-no:0001}")
    private String defaultNetNo;

    /** 个人折扣查询默认网点号 */
    @Value("${bridge.personal-net-no:9998}")
    private String personalNetNo;

    private final FundBaseInfoFacade fundBaseInfoFacade;
    private final FundInfoFacade fundInfoFacade;
    private final OnlineFacade onlineFacade;
    private final FundRetailFacade fundRetailFacade;
    private final FundInfoExtendFacade fundInfoExtendFacade;
    private final FundTrnFacade fundTrnFacade;
    private final FundStatisticsFacade fundStatisticsFacade;
    private final ProductQuotaFacade productQuotaFacade;
    private final WorkdayFacade workdayFacade;
    private final TaInfoFacade taInfoFacade;
    private final FundManagerFacade fundManagerFacade;
    private final ProductManageFacade productManageFacade;
    /** 详情并行拉取（对齐 fims AssistFuture） */
    private final ExecutorService detailExecutor = Executors.newFixedThreadPool(4, r -> {
        Thread t = new Thread(r, "pc-bridge-detail");
        t.setDaemon(true);
        return t;
    });

    public DubboProductQueryService(
            @Qualifier("fundBaseInfoFacade") FundBaseInfoFacade fundBaseInfoFacade,
            @Qualifier("fundInfoFacade") FundInfoFacade fundInfoFacade,
            @Qualifier("onlineFacade") OnlineFacade onlineFacade,
            @Qualifier("fundRetailFacade") FundRetailFacade fundRetailFacade,
            @Qualifier("fundInfoExtendFacade") FundInfoExtendFacade fundInfoExtendFacade,
            @Qualifier("fundTrnFacade") FundTrnFacade fundTrnFacade,
            @Qualifier("fundStatisticsFacade") FundStatisticsFacade fundStatisticsFacade,
            @Qualifier("productQuotaFacade") ProductQuotaFacade productQuotaFacade,
            @Qualifier("workdayFacade") WorkdayFacade workdayFacade,
            @Qualifier("taInfoFacade") TaInfoFacade taInfoFacade,
            @Qualifier("fundManagerFacade") FundManagerFacade fundManagerFacade,
            @Qualifier("productManageFacade") ProductManageFacade productManageFacade) {
        this.fundBaseInfoFacade = fundBaseInfoFacade;
        this.fundInfoFacade = fundInfoFacade;
        this.onlineFacade = onlineFacade;
        this.fundRetailFacade = fundRetailFacade;
        this.fundInfoExtendFacade = fundInfoExtendFacade;
        this.fundTrnFacade = fundTrnFacade;
        this.fundStatisticsFacade = fundStatisticsFacade;
        this.productQuotaFacade = productQuotaFacade;
        this.workdayFacade = workdayFacade;
        this.taInfoFacade = taInfoFacade;
        this.fundManagerFacade = fundManagerFacade;
        this.productManageFacade = productManageFacade;
    }

    @Override
    public Map<String, Object> health() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", "ok");
        m.put("mode", "dubbo");
        m.put("sys_id", sysId);
        m.put("coverage", "fims-detail.app");
        return m;
    }

    /** 客户类型：1个人 / 0机构（交易参数均双查，不偏向任一维度） */
    private static final String CUST_TYPE_PERSONAL = "1";
    private static final String CUST_TYPE_ORG = "0";
    private static final String[] CUST_TYPES_BOTH = {CUST_TYPE_PERSONAL, CUST_TYPE_ORG};
    /** 随 custType/isIns 变化的交易侧字段，从根详情拆到 trade_by_custtype */
    private static final Set<String> CHANNEL_TRADE_KEYS = new LinkedHashSet<>(Arrays.asList(
            "buystatus", "redeemstatus",
            "min_value", "min_value_display", "max_value", "max_value_display",
            "day_max_sum", "day_max_sum_display", "mini_hold_amount",
            "net_sale_flag", "same_day_purchase_flag", "soon_purchase_flag",
            "only_appt_pur_flag"
    ));
    /** 排行基金类型：01股票/02债/03混合/04货币 */
    private static final String[] RANKING_FUND_TYPES = {
            FundRoseReq.FUNDTYPE_GP,
            FundRoseReq.FUNDTYPE_ZQ,
            FundRoseReq.FUNDTYPE_HH,
            FundRoseReq.FUNDTYPE_HB
    };
    private static final int DEFAULT_HISTORY_PAGE_SIZE = 50;

    @Override
    public Map<String, Object> fundDetail(String fundCode) {
        // 默认机构口径，对齐 fims HEADER / 客服主场景
        return fundDetail(fundCode, CUST_TYPE_ORG);
    }

    @Override
    public Map<String, Object> fundDetail(String fundCode, String custType) {
        String normalizedCust = normalizeCustType(custType);
        boolean isOrg = CUST_TYPE_ORG.equals(normalizedCust);
        String custTypeName = isOrg ? "机构" : "个人";

        // ---------- 并行：CMS 主信息 + Retail 确认日 + 运作费率（对齐 fims AssistFuture） ----------
        CompletableFuture<Map<String, Object>> cmsFuture = CompletableFuture.supplyAsync(
                () -> fetchCmsFundDetail(fundCode, isOrg), detailExecutor);
        CompletableFuture<FundInfoRetailBean> retailFuture = CompletableFuture.supplyAsync(
                () -> fetchRetailQuietly(fundCode), detailExecutor);
        CompletableFuture<Map<String, Object>> feeFuture = CompletableFuture.supplyAsync(
                () -> fetchOngoingFeesQuietly(fundCode, normalizedCust), detailExecutor);

        Map<String, Object> cms;
        FundInfoRetailBean retail;
        Map<String, Object> fees;
        try {
            CompletableFuture.allOf(cmsFuture, retailFuture, feeFuture)
                    .get(20, TimeUnit.SECONDS);
            cms = cmsFuture.join();
            retail = retailFuture.join();
            fees = feeFuture.join();
        } catch (Exception ex) {
            throw new IllegalStateException("fundDetail 并行查询失败: " + ex.getMessage(), ex);
        }

        Map<String, Object> detail = new LinkedHashMap<>(cms != null ? cms : Collections.emptyMap());
        // 单客户通道：不再默认双查个人+机构 / 柜台
        for (String key : CHANNEL_TRADE_KEYS) {
            detail.remove(key);
        }
        detail.remove("custtype");
        detail.put("custtype", normalizedCust);
        detail.put("custtype_name", custTypeName);

        Map<String, Object> channel = buildChannelTrade(normalizedCust, custTypeName, cms, null);
        detail.put("trade_by_custtype", Collections.singletonList(channel));
        detail.put("net_no", isOrg ? defaultNetNo : personalNetNo);

        Object mainCode = detail.get("fund_main_code");
        if (mainCode == null || String.valueOf(mainCode).trim().isEmpty()) {
            detail.put("fund_main_code", fundCode);
        }

        // 补确认日 / 交收（Retail 优先，Settlement 兜底）
        mergeAckAndSettlement(detail, fundCode, retail);
        // 补运作费率
        if (fees != null) {
            for (Map.Entry<String, Object> e : fees.entrySet()) {
                putIfAbsent(detail, e.getKey(), e.getValue());
            }
        }
        return detail;
    }

    /** 0=机构 / 1=个人；非法值回落机构。 */
    private static String normalizeCustType(String custType) {
        if (custType == null || custType.trim().isEmpty()) {
            return CUST_TYPE_ORG;
        }
        String t = custType.trim();
        if ("1".equals(t) || "personal".equalsIgnoreCase(t) || "个人".equals(t)) {
            return CUST_TYPE_PERSONAL;
        }
        return CUST_TYPE_ORG;
    }

    private FundInfoRetailBean fetchRetailQuietly(String fundCode) {
        try {
            FundInfoRetailReq req = new FundInfoRetailReq();
            fillHeader(req);
            req.setFundCodeList(Collections.singletonList(fundCode));
            FundInfoRetailResp resp = fundRetailFacade.fundInfoRetail(req);
            if (resp == null || resp.getFundInfoRetailBeans() == null) {
                return null;
            }
            for (FundInfoRetailBean bean : resp.getFundInfoRetailBeans()) {
                if (bean != null && fundCode.equals(bean.getFundCode())) {
                    return bean;
                }
            }
            if (!resp.getFundInfoRetailBeans().isEmpty()) {
                return resp.getFundInfoRetailBeans().get(0);
            }
        } catch (Exception ex) {
            log.warn("fundInfoRetail skip fundCode={} reason={}", fundCode, ex.getMessage());
        }
        return null;
    }

    private Map<String, Object> fetchOngoingFeesQuietly(String fundCode, String custType) {
        Map<String, Object> out = new LinkedHashMap<>();
        try {
            FundFeeRateReq req = new FundFeeRateReq();
            fillHeader(req);
            req.setFundcode(fundCode.trim());
            req.setCustomerType(custType);
            FundFeeRateResp resp = fundInfoFacade.queryFundFeeRate(req);
            if (resp != null && isSuccess(resp.getRespCode())) {
                putIfAbsent(out, "manage_ratio", resp.getManagerate());
                putIfAbsent(out, "hosting_rate", resp.getCustodyrate());
                putIfAbsent(out, "service_rate", resp.getSaleServiceRate());
                putIfAbsent(out, "spcl_manage_rate_desc", resp.getSpclManagerateDesc());
                putIfAbsent(out, "spcl_redeem_desc", resp.getSpclRedeemDesc());
            }
        } catch (Exception ex) {
            log.warn("queryFundFeeRate skip fundCode={} reason={}", fundCode, ex.getMessage());
        }
        return out;
    }

    /**
     * 对齐 fims / 机构慧：
     * - 交易确认天数：Retail.confirmDays/subDays 优先（HEADER buyConfirmNDisplay）
     * - 最快赎回到账：Settlement.redeemAccount_n（settle_days+确认天数，与 getRedemptionCashDate 同公式）
     *   用实时交收覆盖 CMS 详情 Redis 缓存里可能过期的 redemptionCashDate，避免同基金偶发 T+2/T+4 抖动。
     */
    private void mergeAckAndSettlement(
            Map<String, Object> detail, String fundCode, FundInfoRetailBean retail) {
        if (retail != null) {
            if (retail.getConfirmDays() != null) {
                putIfAbsent(detail, "buy_ack_days", String.valueOf(retail.getConfirmDays()));
                putIfAbsent(detail, "redeem_ack_days", String.valueOf(retail.getConfirmDays()));
            }
            if (retail.getSubDays() != null) {
                putIfAbsent(detail, "sub_ack_days", String.valueOf(retail.getSubDays()));
            }
        }
        try {
            SettlementDateRequest req = new SettlementDateRequest();
            req.setReqId(sysId + UUID.randomUUID());
            req.setFundcode(fundCode);
            req.setNetNo(defaultNetNo);
            SettlementDateResponse resp = onlineFacade.querySettlementDate(req);
            if (resp != null && Boolean.TRUE.equals(resp.getIsSuccess())) {
                putIfAbsent(detail, "buy_ack_days", resp.getBuyConfirm_n());
                putIfAbsent(detail, "redeem_ack_days", resp.getRedeemConfirm_n());
                putIfAbsent(detail, "sub_ack_days", resp.getSubscribeConfirmDays());
                String cashN = resp.getRedeemAccount_n();
                if (cashN != null && !cashN.trim().isEmpty()) {
                    String cash = cashN.trim().toUpperCase().startsWith("T+")
                            ? cashN.trim()
                            : "T+" + cashN.trim();
                    Object cmsCash = detail.get("redemption_cash_date");
                    if (cmsCash != null && !cash.equals(String.valueOf(cmsCash).trim())) {
                        log.info(
                                "redemption_cash_date override fundCode={} cms={} settlement={}",
                                fundCode, cmsCash, cash);
                    }
                    // 覆盖 CMS：交收为实时查询，避免 FUND_BASEINFO_PC_* 缓存导致到账日抖动
                    detail.put("redemption_cash_date", cash);
                }
            }
        } catch (Exception ex) {
            log.warn("querySettlementDate skip fundCode={} reason={}", fundCode, ex.getMessage());
        }
    }

    @Override
    public List<Map<String, Object>> listByMainFundCode(String mainFundCode) {
        if (mainFundCode == null || mainFundCode.trim().isEmpty()) {
            return Collections.emptyList();
        }
        QueryMainFundRequest req = new QueryMainFundRequest();
        fillOnlineHeader(req);
        req.setMainFundCode(mainFundCode.trim());
        req.setNetNo(defaultNetNo);
        QueryMainFundResponse resp = onlineFacade.queryFundByMainFund(req);
        ensureOnlineOk(resp, "queryFundByMainFund");
        List<FundInfoBean> beans = resp.getFundCodeList();
        if (beans == null || beans.isEmpty()) {
            return Collections.emptyList();
        }
        List<Map<String, Object>> out = new ArrayList<>(beans.size());
        for (FundInfoBean bean : beans) {
            Map<String, Object> row = ProductFieldMapper.fromMainFundBean(bean);
            if (!row.isEmpty()) {
                out.add(row);
            }
        }
        return out;
    }

    /**
     * 补齐运作费率：管理费 / 托管费 / 销售服务费。
     * 对齐 PC {@code FundInfoFacade.queryFundFeeRate} → {@code FundInfoUtil.parseFeeRate}。
     */
    private void mergeOngoingFeesFromFeeRateApi(Map<String, Object> detail, String fundCode) {
        if (detail == null || fundCode == null || fundCode.trim().isEmpty()) {
            return;
        }
        // 先个人后机构（接口 custType 空时默认机构）
        for (String custType : CUST_TYPES_BOTH) {
            try {
                FundFeeRateReq req = new FundFeeRateReq();
                fillHeader(req);
                req.setFundcode(fundCode.trim());
                req.setCustomerType(custType);
                FundFeeRateResp resp = fundInfoFacade.queryFundFeeRate(req);
                if (resp == null || !isSuccess(resp.getRespCode())) {
                    continue;
                }
                putIfAbsent(detail, "manage_ratio", resp.getManagerate());
                putIfAbsent(detail, "hosting_rate", resp.getCustodyrate());
                putIfAbsent(detail, "service_rate", resp.getSaleServiceRate());
                putIfAbsent(detail, "spcl_manage_rate_desc", resp.getSpclManagerateDesc());
                putIfAbsent(detail, "spcl_redeem_desc", resp.getSpclRedeemDesc());
                // 已补到关键字段即可结束；否则换客户类型再试
                if (detail.get("manage_ratio") != null
                        || detail.get("hosting_rate") != null
                        || detail.get("service_rate") != null) {
                    return;
                }
            } catch (Exception ignored) {
                // 运作费率补充失败不影响主详情
            }
        }
    }

    private static void putIfAbsent(Map<String, Object> target, String key, Object value) {
        if (value == null || target == null || key == null) {
            return;
        }
        Object existing = target.get(key);
        if (existing == null) {
            target.put(key, value);
        }
    }

    private Map<String, Object> fetchCmsFundDetail(String fundCode, boolean isIns) {
        FundInfoReq req = new FundInfoReq();
        fillHeader(req);
        req.setFundcode(fundCode);
        if (isIns) {
            req.setIsIns("Y");
        }
        FundInfoResp resp = fundInfoFacade.queryFundInfo(req);
        ensureOk(resp, isIns ? "queryFundInfo/org" : "queryFundInfo/personal");
        return ProductFieldMapper.fundDetailFromCms(resp);
    }

    private Map<String, Object> fetchCounterDetail(String fundCode, String custType) {
        try {
            FundBaseInfoDetailReq baseReq = newDetailReq(fundCode, custType);
            FundInfoBaseDetailResp base = fundBaseInfoFacade.fundBaseInfoDetail(baseReq);
            if (base != null && isSuccess(base.getRespCode())) {
                return ProductFieldMapper.fundDetail(base);
            }
        } catch (Exception ignored) {
            // 柜台补充失败不影响主详情
        }
        return Collections.emptyMap();
    }

    private static Map<String, Object> mergeProductFields(Map<String, Object> a, Map<String, Object> b) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (a != null) {
            out.putAll(a);
        }
        if (b != null) {
            for (Map.Entry<String, Object> e : b.entrySet()) {
                if (!out.containsKey(e.getKey()) && e.getValue() != null) {
                    out.put(e.getKey(), e.getValue());
                }
            }
        }
        return out;
    }

    private static void mergeMissing(Map<String, Object> target, Map<String, Object> extra) {
        if (extra == null || extra.isEmpty()) {
            return;
        }
        for (Map.Entry<String, Object> e : extra.entrySet()) {
            if (CHANNEL_TRADE_KEYS.contains(e.getKey())) {
                continue;
            }
            if (!target.containsKey(e.getKey()) && e.getValue() != null) {
                target.put(e.getKey(), e.getValue());
            }
        }
    }

    private static Map<String, Object> buildChannelTrade(
            String custType,
            String custTypeName,
            Map<String, Object> cms,
            Map<String, Object> counter) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("custtype", custType);
        row.put("custtype_name", custTypeName);
        for (String key : CHANNEL_TRADE_KEYS) {
            Object val = cms != null ? cms.get(key) : null;
            if (val == null && counter != null) {
                val = counter.get(key);
            }
            // 申购/赎回等关键态：空也写出，避免缺键被当成可买
            if ("buystatus".equals(key) || "redeemstatus".equals(key)) {
                row.put(key, val != null ? val : "");
            } else if (val != null) {
                row.put(key, val);
            }
        }
        return row;
    }

    @Override
    public Map<String, Object> fundArchive(String fundCode) {
        FundArchiveInfoReq req = new FundArchiveInfoReq();
        fillHeader(req);
        req.setFundcode(fundCode);
        FundArchiveInfoResp resp = fundInfoFacade.querFundArchiveInfo(req);
        ensureOk(resp, "querFundArchiveInfo");
        return ProductFieldMapper.archive(resp);
    }

    @Override
    public Map<String, Object> fundRiskIndex(String fundCode) {
        FundRiskIndexReq req = new FundRiskIndexReq();
        fillHeader(req);
        req.setFundCode(fundCode);
        FundRiskIndexResp resp = fundInfoExtendFacade.queryFundRiskIndex(req);
        ensureOk(resp, "queryFundRiskIndex");
        return ProductFieldMapper.riskIndex(resp);
    }

    @Override
    public Map<String, Object> fundHoldings(String fundCode) {
        FundAssetPositionReq req = new FundAssetPositionReq();
        fillHeader(req);
        req.setFundcode(fundCode);
        FundAssetPositionResp resp = fundInfoFacade.queryFundAssetPosition(req);
        ensureOk(resp, "queryFundAssetPosition");
        return ProductFieldMapper.holdings(resp);
    }

    @Override
    public List<Map<String, Object>> fundAssetScale(String fundCode) {
        FundAssetScaleReq req = new FundAssetScaleReq();
        fillHeader(req);
        req.setFundCode(fundCode);
        req.setPageNum(1);
        req.setPageSize(DEFAULT_HISTORY_PAGE_SIZE);
        FundAssetScaleResp resp = fundInfoExtendFacade.queryFundAssetScaleList(req);
        ensureOk(resp, "queryFundAssetScaleList");
        return ProductFieldMapper.assetScaleRows(fundCode, resp.getFundAssetScaleBeanList());
    }

    @Override
    public List<Map<String, Object>> fundHolders(String fundCode) {
        FundHolderInfoReq req = new FundHolderInfoReq();
        fillHeader(req);
        req.setFundCode(fundCode);
        req.setPageNum(1);
        req.setPageSize(DEFAULT_HISTORY_PAGE_SIZE);
        FundHolderInfoResp resp = fundInfoExtendFacade.queryFundHolderInfoList(req);
        ensureOk(resp, "queryFundHolderInfoList");
        return ProductFieldMapper.holderRows(fundCode, resp.getFundHolderInfoBeanList());
    }

    @Override
    public List<Map<String, Object>> fundDividends(String fundCode) {
        FundDividendReq req = new FundDividendReq();
        fillHeader(req);
        req.setFundcode(fundCode);
        req.setPageNum(1);
        req.setPageSize(DEFAULT_HISTORY_PAGE_SIZE);
        FundDividendResp resp = fundInfoFacade.queryDividendDistrByFundCode(req);
        ensureOk(resp, "queryDividendDistrByFundCode");
        return ProductFieldMapper.dividendRows(fundCode, resp.getFundDividendDistrList());
    }

    @Override
    public Map<String, Object> fundInvestStyle(String fundCode) {
        FundInvestStyleReq req = new FundInvestStyleReq();
        fillHeader(req);
        req.setFundCode(fundCode);
        FundInvestStyleResp resp = fundInfoExtendFacade.queryFundInvestStyle(req);
        ensureOk(resp, "queryFundInvestStyle");
        return ProductFieldMapper.investStyle(resp);
    }

    @Override
    public List<Map<String, Object>> feeRates(String fundCode) {
        // PC SQL：不传 custType 会混个人/机构且无标签；分别查 1/0 并标注
        List<Map<String, Object>> out = new ArrayList<>();
        for (String custType : CUST_TYPES_BOTH) {
            FundBaseInfoDetailReq req = newDetailReq(fundCode, custType);
            FundFareRatioResp resp = fundTrnFacade.getFundFareRatioInfo(req);
            ensureOk(resp, "getFundFareRatioInfo/" + custType);
            out.addAll(ProductFieldMapper.feeRates(fundCode, resp.getRateSubList(), "认购费", custType));
            out.addAll(ProductFieldMapper.feeRates(fundCode, resp.getRateApplyList(), "申购费", custType));
            out.addAll(ProductFieldMapper.feeRates(fundCode, resp.getRateRedeemList(), "赎回费", custType));
        }
        return out;
    }

    @Override
    public List<Map<String, Object>> tradeLimits(String fundCode) {
        // SQL 按 CUSTTYPE 过滤；个人 + 机构双查
        List<Map<String, Object>> out = new ArrayList<>();
        for (String custType : CUST_TYPES_BOTH) {
            FundTradeLimitReq req = new FundTradeLimitReq();
            fillHeader(req);
            req.setFundCode(fundCode);
            req.setCusType(custType);
            FundTradeLimitResp resp = fundTrnFacade.getFundTradeLimit(req);
            ensureOk(resp, "getFundTradeLimit/" + custType);
            if (resp.getTradeLimitList() != null) {
                for (TradeLimitBean bean : resp.getTradeLimitList()) {
                    Map<String, Object> row = ProductFieldMapper.tradeLimit(bean);
                    if (!row.containsKey("custtype")) {
                        row.put("custtype", custType);
                    }
                    out.add(row);
                }
            }
        }
        return out;
    }

    @Override
    public List<Map<String, Object>> discounts(String fundCode) {
        // 机构折扣（网点=0001）+ 个人折扣（网点=9998）；两侧都带上
        List<Map<String, Object>> out = new ArrayList<>();

        // 机构折扣：网点 0001
        FundBaseInfoDetailReq orgReq = newDetailReq(fundCode, null);
        FundDiscountResp orgResp = fundTrnFacade.getFundDiscount(orgReq);
        ensureOk(orgResp, "getFundDiscount(org)");
        appendDiscounts(out, fundCode, orgResp.getOrgDiscountDetailList(), CUST_TYPE_ORG);

        // 个人折扣：网点 9998
        FundBaseInfoDetailReq perReq = newDetailReqWithNetCode(fundCode, null, personalNetNo);
        FundDiscountResp perResp = fundTrnFacade.getFundDiscount(perReq);
        ensureOk(perResp, "getFundDiscount(personal)");
        appendDiscounts(out, fundCode, perResp.getPerApplyList(), CUST_TYPE_PERSONAL);
        appendDiscounts(out, fundCode, perResp.getPerSubList(), CUST_TYPE_PERSONAL);

        return out;
    }

    @Override
    public List<Map<String, Object>> feeRatesBatch(List<String> fundCodes) {
        return batchCollect(fundCodes, this::feeRates, "feeRates");
    }

    @Override
    public List<Map<String, Object>> tradeLimitsBatch(List<String> fundCodes) {
        return batchCollect(fundCodes, this::tradeLimits, "tradeLimits");
    }

    @Override
    public List<Map<String, Object>> discountsBatch(List<String> fundCodes) {
        return batchCollect(fundCodes, this::discounts, "discounts");
    }

    private List<Map<String, Object>> batchCollect(
            List<String> fundCodes,
            java.util.function.Function<String, List<Map<String, Object>>> loader,
            String api) {
        List<Map<String, Object>> out = new ArrayList<>();
        for (String code : normalizeFundCodes(fundCodes)) {
            try {
                List<Map<String, Object>> rows = loader.apply(code);
                if (rows != null && !rows.isEmpty()) {
                    out.addAll(rows);
                }
            } catch (Exception ex) {
                log.info("{} batch skip fundCode={} reason={}", api, code, ex.getMessage());
            }
        }
        return out;
    }

    private static List<String> normalizeFundCodes(List<String> fundCodes) {
        if (fundCodes == null || fundCodes.isEmpty()) {
            return Collections.emptyList();
        }
        LinkedHashSet<String> uniq = new LinkedHashSet<>();
        for (String raw : fundCodes) {
            if (raw == null) {
                continue;
            }
            for (String part : raw.split("[,，\\s]+")) {
                String code = part.trim();
                if (!code.isEmpty()) {
                    uniq.add(code);
                }
            }
        }
        List<String> list = new ArrayList<>(uniq);
        if (list.size() > 10) {
            return list.subList(0, 10);
        }
        return list;
    }

    /**
     * 对齐 productcenter FundTrnServiceImpl#getFundOpenPeriod：
     * - 不传 businessCode 时服务端只查 020（认购）进 subFundOpenPeriod；
     * - 传 businessCode 才会按该业务码分页查 fundOpenPeriodVos。
     * 因此客服侧默认分别查 020/022/024 后合并。
     */
    private static final String[] OPEN_PERIOD_BUSIN_CODES = {"020", "022", "024"};

    @Override
    public List<Map<String, Object>> openPeriods(String fundCode, String businessCode) {
        List<String> codes;
        if (businessCode != null && !businessCode.trim().isEmpty()) {
            codes = Collections.singletonList(businessCode.trim());
        } else {
            codes = Arrays.asList(OPEN_PERIOD_BUSIN_CODES);
        }
        List<Map<String, Object>> out = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        for (String code : codes) {
            appendOpenPeriodsForBusin(out, seen, fundCode, code);
        }
        return out;
    }

    private void appendOpenPeriodsForBusin(
            List<Map<String, Object>> out,
            Set<String> seen,
            String fundCode,
            String businCode) {
        FundOpenPeriodTrnReq req = new FundOpenPeriodTrnReq();
        fillHeader(req);
        req.setFundCode(fundCode);
        req.setBusinessCode(businCode);
        req.setPageNum(1);
        req.setPageSize(50);
        FundOpenPeriodTrnResp resp = fundTrnFacade.getFundOpenPeriod(req);
        ensureOk(resp, "getFundOpenPeriod/" + businCode);
        if (resp.getFundOpenPeriodVos() != null) {
            for (FundOpenPeriodTrn p : resp.getFundOpenPeriodVos()) {
                addOpenPeriodRow(out, seen, p);
            }
        }
        // 020 时服务端还会填 subFundOpenPeriod（取认购开放期首条）；vos 为空时兜底
        if ("020".equals(businCode) && resp.getSubFundOpenPeriod() != null) {
            addOpenPeriodRow(out, seen, resp.getSubFundOpenPeriod());
        }
    }

    private static void addOpenPeriodRow(
            List<Map<String, Object>> out, Set<String> seen, FundOpenPeriodTrn p) {
        if (p == null) {
            return;
        }
        Map<String, Object> row = ProductFieldMapper.openPeriod(p);
        String key = String.valueOf(row.get("busincode"))
                + "|" + row.get("trade_start_date")
                + "|" + row.get("trade_end_date")
                + "|" + row.get("appt_start_time")
                + "|" + row.get("appt_end_time");
        if (seen.add(key)) {
            out.add(row);
        }
    }

    @Override
    public Map<String, Object> performance(String fundCode) {
        // 对齐 fims：queryFundRankingInfo；优先 V2
        FundRankingInfoReq req = new FundRankingInfoReq();
        fillHeader(req);
        req.setFundcode(fundCode);
        FundRankingInfoResp resp;
        try {
            resp = fundStatisticsFacade.queryFundRankingInfoV2(req);
            ensureOk(resp, "queryFundRankingInfoV2");
        } catch (Exception ex) {
            resp = fundStatisticsFacade.queryFundRankingInfo(req);
            ensureOk(resp, "queryFundRankingInfo");
        }
        return ProductFieldMapper.performanceFromRanking(fundCode, resp.getFundrankinglist());
    }

    @Override
    public List<Map<String, Object>> ranking(String orderBy, String direction, int limit, String fundType) {
        String field = orderBy == null || orderBy.isEmpty() ? "thisyear_rose" : orderBy;
        int pageSize = Math.max(1, Math.min(limit, 50));
        boolean asc = "asc".equalsIgnoreCase(direction);
        List<String> types = resolveRankingFundTypes(fundType);
        List<Map<String, Object>> out = new ArrayList<>();
        for (String type : types) {
            FundRoseReq req = new FundRoseReq();
            fillHeader(req);
            req.setFundtype(type);
            req.setOrderby(asc ? FundRoseReq.ORDERBY_ZX : FundRoseReq.ORDERBY_DX);
            req.setDatetype(mapOrderByToDateInterval(orderBy));
            req.setCurrentpage(1);
            // 多类型合并时每类多取一些，最后再截断
            req.setPagesize(types.size() == 1 ? pageSize : pageSize);
            req.setState("0");
            FundRoseResp resp = fundStatisticsFacade.queryFundRoseList(req);
            ensureOk(resp, "queryFundRoseList/" + type);
            if (resp.getFundroselist() != null) {
                for (FundRose rose : resp.getFundroselist()) {
                    out.add(ProductFieldMapper.roseRow(rose, field, type));
                }
            }
        }
        if (types.size() > 1) {
            out.sort((a, b) -> compareRose(a.get(field), b.get(field), asc));
            if (out.size() > pageSize) {
                out = new ArrayList<>(out.subList(0, pageSize));
            }
        }
        return out;
    }

    private static List<String> resolveRankingFundTypes(String fundType) {
        if (fundType != null && !fundType.trim().isEmpty()) {
            String t = fundType.trim();
            // 兼容名称简写
            if ("股票".equals(t) || "gp".equalsIgnoreCase(t)) {
                t = FundRoseReq.FUNDTYPE_GP;
            } else if ("债券".equals(t) || "zq".equalsIgnoreCase(t)) {
                t = FundRoseReq.FUNDTYPE_ZQ;
            } else if ("混合".equals(t) || "hh".equalsIgnoreCase(t)) {
                t = FundRoseReq.FUNDTYPE_HH;
            } else if ("货币".equals(t) || "hb".equalsIgnoreCase(t)) {
                t = FundRoseReq.FUNDTYPE_HB;
            }
            return Collections.singletonList(t);
        }
        return Arrays.asList(RANKING_FUND_TYPES);
    }

    private static int compareRose(Object a, Object b, boolean asc) {
        BigDecimal da = toBigDecimal(a);
        BigDecimal db = toBigDecimal(b);
        if (da == null && db == null) {
            return 0;
        }
        if (da == null) {
            return 1;
        }
        if (db == null) {
            return -1;
        }
        int cmp = da.compareTo(db);
        return asc ? cmp : -cmp;
    }

    private static BigDecimal toBigDecimal(Object v) {
        if (v == null) {
            return null;
        }
        if (v instanceof BigDecimal) {
            return (BigDecimal) v;
        }
        if (v instanceof Number) {
            return BigDecimal.valueOf(((Number) v).doubleValue());
        }
        try {
            return new BigDecimal(String.valueOf(v));
        } catch (Exception ex) {
            return null;
        }
    }

    @Override
    public Map<String, Object> quotaInfo(String fundCode, String netNo) {
        String resolvedNetNo = resolveNetNo(netNo);
        QueryQuotaRequest req = newQuotaReq(fundCode, resolvedNetNo);
        try {
            QueryQuotaResp resp = productQuotaFacade.queryQuotaInfo(req);
            ensureOk(resp, "queryQuotaInfo");
            Map<String, Object> mapped = ProductFieldMapper.quotaInfo(resp);
            mapped.put("net_no", resolvedNetNo);
            mapped.put("quota_configured", true);
            return mapped;
        } catch (Exception ex) {
            // 公募等产品中心常无额度配置，抛 BizException「未查询到相应的额度信息」；
            // 网关容错为空结果，避免 500 并减少下游误报为硬失败。
            return emptyQuotaInfo(fundCode, resolvedNetNo, ex);
        }
    }

    @Override
    public List<Map<String, Object>> quotaControls(String fundCode, String netNo) {
        String resolvedNetNo = resolveNetNo(netNo);
        QueryQuotaRequest req = newQuotaReq(fundCode, resolvedNetNo);
        try {
            QueryQuotaConfigResp resp = productQuotaFacade.queryQuotaConfig(req);
            ensureOk(resp, "queryQuotaConfig");
            List<Map<String, Object>> rows =
                    ProductFieldMapper.quotaControlRows(fundCode, resp.getQuotaConfigBeans());
            if (rows.isEmpty()) {
                Map<String, Object> info = quotaInfo(fundCode, resolvedNetNo);
                if (info.isEmpty() || Boolean.FALSE.equals(info.get("quota_configured"))) {
                    return Collections.emptyList();
                }
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("fund_code", fundCode);
                row.put("net_no", resolvedNetNo);
                row.put("note", "无网点专享明细，以下为总控摘要");
                if (info.get("share_quota") != null) {
                    row.put("share_quota", info.get("share_quota"));
                }
                return Collections.singletonList(row);
            }
            return rows;
        } catch (Exception ex) {
            log.info(
                    "quotaControls empty fallback fundCode={} netNo={} reason={}",
                    fundCode,
                    resolvedNetNo,
                    ex.getMessage());
            return Collections.emptyList();
        }
    }

    private static Map<String, Object> emptyQuotaInfo(String fundCode, String netNo, Exception ex) {
        log.info(
                "quotaInfo empty fallback fundCode={} netNo={} reason={}",
                fundCode,
                netNo,
                ex != null ? ex.getMessage() : null);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("fund_code", fundCode);
        m.put("net_no", netNo);
        m.put("quota_configured", false);
        m.put(
                "note",
                "未查询到产品额度配置（公募通常不配额度；仅私募等产品可能有总控/专享名额）");
        return m;
    }

    private QueryQuotaRequest newQuotaReq(String fundCode, String netNo) {
        QueryQuotaRequest req = new QueryQuotaRequest();
        fillHeader(req);
        req.setFundCode(fundCode);
        req.setNetNo(netNo);
        return req;
    }

    private String resolveNetNo(String netNo) {
        if (netNo != null && !netNo.trim().isEmpty()) {
            return netNo.trim();
        }
        return defaultNetNo;
    }

    @Override
    public Map<String, Object> currentWorkday() {
        QueryCurrentBusinessDateReq req = new QueryCurrentBusinessDateReq();
        fillHeader(req);
        QueryCurrentBusinessDateResp resp = workdayFacade.queryCurrentBusinessDate(req);
        ensureOk(resp, "queryCurrentBusinessDate");
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("workday", resp.getCurrentBusinessDate());
        m.put("curworkdate", resp.getCurrentBusinessDate());
        return m;
    }

    @Override
    public List<Map<String, Object>> workdayList(String startDate, String endDate) {
        WorkdayDifferenceReq req = new WorkdayDifferenceReq();
        fillHeader(req);
        req.setStartDate(normalizeYmd(startDate));
        req.setEndDate(normalizeYmd(endDate));
        QueryWorkdayListResp resp = workdayFacade.queryWorkdayList(req);
        ensureOk(resp, "queryWorkdayList");
        List<Map<String, Object>> out = new ArrayList<>();
        if (resp.getWorkdays() != null) {
            for (String day : resp.getWorkdays()) {
                if (day == null || day.trim().isEmpty()) {
                    continue;
                }
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("workday", day.trim());
                row.put("start_date", req.getStartDate());
                row.put("end_date", req.getEndDate());
                out.add(row);
            }
        }
        return out;
    }

    @Override
    public Map<String, Object> checkWorkday(String date) {
        CheckIsBusinessDayReq req = new CheckIsBusinessDayReq();
        fillHeader(req);
        req.setDate(normalizeYmd(date));
        CheckIsBusinessDayResp resp = workdayFacade.checkIsBusinessDay(req);
        ensureOk(resp, "checkIsBusinessDay");
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("date", req.getDate());
        m.put("is_business_day", Boolean.TRUE.equals(resp.getIsBusinessDay()));
        return m;
    }

    private static String normalizeYmd(String raw) {
        if (raw == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (c >= '0' && c <= '9') {
                sb.append(c);
            }
        }
        String digits = sb.toString();
        if (digits.length() >= 8) {
            return digits.substring(0, 8);
        }
        return raw.trim();
    }

    @Override
    public Map<String, Object> taInfo(String taCode) {
        TaInfoReq req = new TaInfoReq();
        fillHeader(req);
        req.setTaCode(taCode);
        TaInfoResp resp = taInfoFacade.queryAllTaInfo(req);
        ensureOk(resp, "queryAllTaInfo");
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("tacode", taCode);
        if (resp.getTaInfoList() != null) {
            for (TaInfo info : resp.getTaInfoList()) {
                if (info != null && taCode.equals(info.getTaCode())) {
                    return ProductFieldMapper.taInfo(info);
                }
            }
        }
        return m;
    }

    @Override
    public List<Map<String, Object>> searchTa(String keyword, int limit) {
        String key = normalizeKeyword(keyword);
        if (key.isEmpty()) {
            return Collections.emptyList();
        }
        TaInfoReq req = new TaInfoReq();
        fillHeader(req);
        // 名称或代码模糊（PC: taCodeOrName like）
        req.setTaCodeOrName(key);
        TaInfoResp resp = taInfoFacade.queryAllTaInfo(req);
        ensureOk(resp, "queryAllTaInfo/search");
        List<Map<String, Object>> out = new ArrayList<>();
        int max = Math.max(1, Math.min(limit, 50));
        if (resp.getTaInfoList() != null) {
            for (TaInfo info : resp.getTaInfoList()) {
                if (info == null) {
                    continue;
                }
                out.add(ProductFieldMapper.taInfo(info));
                if (out.size() >= max) {
                    break;
                }
            }
        }
        return out;
    }

    @Override
    public Map<String, Object> managerOrg(String managerCode) {
        QueryFundManagerNameReq req = new QueryFundManagerNameReq();
        fillHeader(req);
        req.setManagerCodeList(Collections.singletonList(managerCode));
        QueryFundManagerNameResp resp = fundManagerFacade.queryFundManagerName(req);
        ensureOk(resp, "queryFundManagerName");
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("manager_code", managerCode);
        if (resp.getManagerMap() != null && resp.getManagerMap().containsKey(managerCode)) {
            m.put("manager_name", resp.getManagerMap().get(managerCode));
        }
        return m;
    }

    @Override
    public List<Map<String, Object>> searchManagers(String keyword, int limit) {
        String key = normalizeKeyword(keyword);
        if (key.isEmpty()) {
            return Collections.emptyList();
        }
        QueryFundManagerListReq req = new QueryFundManagerListReq();
        fillHeader(req);
        req.setManager(key);
        QueryFundManagerListResp resp = productManageFacade.queryFundManagerList(req);
        ensureOk(resp, "queryFundManagerList");
        List<Map<String, Object>> out = new ArrayList<>();
        int max = Math.max(1, Math.min(limit, 50));
        if (resp.getManagerInfoList() != null) {
            for (com.leadbank.productcenter.facade.bean.product.FundManagerBean bean
                    : resp.getManagerInfoList()) {
                if (bean == null) {
                    continue;
                }
                out.add(ProductFieldMapper.managerOrgRow(bean));
                if (out.size() >= max) {
                    break;
                }
            }
        }
        return out;
    }

    @Override
    public List<Map<String, Object>> searchFunds(
            String keyword,
            int limit,
            String managerCode,
            String taCode,
            String consignFlag,
            String fundType,
            String fundState,
            String productType) {
        String key = normalizeKeyword(keyword);
        boolean hasFilter =
                !isBlank(managerCode)
                        || !isBlank(taCode)
                        || !isBlank(consignFlag)
                        || !isBlank(fundType)
                        || !isBlank(fundState)
                        || !isBlank(productType);
        if (key.isEmpty() && !hasFilter) {
            return Collections.emptyList();
        }
        int want = Math.max(1, Math.min(limit, 50));
        // 本地过滤前多取一些，避免筛后不足
        int pageSize = Math.min(100, Math.max(want * 5, want));
        FundBaseInfoPageReq req = new FundBaseInfoPageReq();
        fillHeader(req);
        if (!key.isEmpty()) {
            // SQL: FUND_NAME/CODE like %fundName%
            req.setFundName(key);
        }
        // 产品中心 PageReq 已确认支持的筛选项（其余仍走本地过滤）
        if (!isBlank(managerCode)) {
            req.setManagerCode(managerCode.trim());
        }
        if (!isBlank(taCode)) {
            req.setTaCode(taCode.trim());
        }
        if (!isBlank(consignFlag)) {
            req.setConsignFlag2(consignFlag.trim());
        }
        if (!isBlank(fundType)) {
            req.setFundType(fundType.trim());
        }
        if (!isBlank(fundState)) {
            req.setFundState(fundState.trim());
        }
        if (!isBlank(productType)) {
            req.setProductType(productType.trim());
        }
        req.setPageNum(1);
        req.setPageSize(pageSize);
        FundInfoBasePageResp resp = fundBaseInfoFacade.fundBaseInfoPage(req);
        ensureOk(resp, "fundBaseInfoPage");
        List<Map<String, Object>> out = new ArrayList<>();
        if (resp.getFundInfoBaseBeans() != null) {
            for (FundInfoBasePageBean bean : resp.getFundInfoBaseBeans()) {
                if (bean == null) {
                    continue;
                }
                Map<String, Object> row = ProductFieldMapper.fundSearchRow(bean);
                if (!matchEq(row, "manager_code", managerCode)) {
                    continue;
                }
                if (!matchEq(row, "tacode", taCode)) {
                    continue;
                }
                if (!matchEq(row, "consign_flag", consignFlag)) {
                    continue;
                }
                if (!matchEq(row, "fund_type", fundType)) {
                    continue;
                }
                if (!matchEq(row, "fundstate", fundState)) {
                    continue;
                }
                if (!matchEq(row, "product_type", productType)) {
                    continue;
                }
                out.add(row);
                if (out.size() >= want) {
                    break;
                }
            }
        }
        return out;
    }

    private static boolean isBlank(String s) {
        return s == null || s.trim().isEmpty();
    }

    private static boolean matchEq(Map<String, Object> row, String field, String expect) {
        if (isBlank(expect)) {
            return true;
        }
        Object actual = row.get(field);
        return actual != null && expect.trim().equals(String.valueOf(actual).trim());
    }

    private static String normalizeKeyword(String keyword) {
        if (keyword == null) {
            return "";
        }
        String key = keyword.trim();
        // 去掉 LLM 可能自带的 % 通配符，由 PC SQL 统一加前后 %
        while (key.startsWith("%")) {
            key = key.substring(1);
        }
        while (key.endsWith("%")) {
            key = key.substring(0, key.length() - 1);
        }
        return key.trim();
    }

    private FundBaseInfoDetailReq newDetailReq(String fundCode, String custType) {
        FundBaseInfoDetailReq req = new FundBaseInfoDetailReq();
        fillHeader(req);
        req.setFundCode(fundCode);
        if (custType != null && !custType.isEmpty()) {
            req.setCustType(custType);
        }
        req.setNetCode(defaultNetNo);
        return req;
    }

    private FundBaseInfoDetailReq newDetailReqWithNetCode(String fundCode, String custType, String netCode) {
        FundBaseInfoDetailReq req = new FundBaseInfoDetailReq();
        fillHeader(req);
        req.setFundCode(fundCode);
        if (custType != null && !custType.isEmpty()) {
            req.setCustType(custType);
        }
        req.setNetCode(netCode);
        return req;
    }

    private void fillHeader(AbstractRequest req) {
        req.setReqId(sysId + UUID.randomUUID());
        req.setSysId(sysId);
        req.setVersion(apiVersion);
        req.setDateTime(new Date());
    }

    private void fillHeader(com.leadbank.productcenter.facade.abs.AbstractRequest req) {
        req.setReqId(sysId + UUID.randomUUID());
        req.setSysId(sysId);
        req.setAppId(sysId);
        req.setVersion(apiVersion);
        req.setDateTime(new java.sql.Timestamp(System.currentTimeMillis()));
    }

    /** OnlineFacade 请求头（leadbank.common AbstractQueryRequest） */
    private void fillOnlineHeader(AbstractOnlineQueryRequest req) {
        req.setReqId(sysId + UUID.randomUUID());
        req.setSysId(sysId);
        req.setAppId(sysId);
        req.setVersion(apiVersion);
        req.setDateTime(new Date());
    }

    private void ensureOk(AbstractResponse resp, String api) {
        if (resp == null) {
            throw new IllegalStateException(api + " 返回为空");
        }
        String code = resp.getRespCode();
        if (!isSuccess(code)) {
            throw new IllegalStateException(api + " 失败: respCode=" + code + " msg=" + resp.getRespMessage());
        }
    }

    private void ensureOk(com.leadbank.productcenter.facade.abs.AbstractResponse resp, String api) {
        if (resp == null) {
            throw new IllegalStateException(api + " 返回为空");
        }
        String code = resp.getRespCode();
        if (!isSuccess(code)) {
            throw new IllegalStateException(api + " 失败: respCode=" + code + " msg=" + resp.getRespMessage());
        }
    }

    private void ensureOnlineOk(FundOnlineAbsResponse resp, String api) {
        if (resp == null) {
            throw new IllegalStateException(api + " 返回为空");
        }
        if (Boolean.FALSE.equals(resp.getIsSuccess())) {
            throw new IllegalStateException(
                    api + " 失败: returnCode=" + resp.getReturnCode() + " msg=" + resp.getReturnMessage());
        }
        String code = resp.getReturnCode();
        if (code != null && !code.isEmpty() && !isOnlineSuccess(code)) {
            throw new IllegalStateException(
                    api + " 失败: returnCode=" + code + " msg=" + resp.getReturnMessage());
        }
    }

    private static boolean isOnlineSuccess(String code) {
        return "0000".equals(code)
                || "S".equalsIgnoreCase(code)
                || "000000".equals(code)
                || "0".equals(code)
                || "success".equalsIgnoreCase(code);
    }

    private static boolean isSuccess(String code) {
        if (code == null) {
            return false;
        }
        return "S".equalsIgnoreCase(code)
                || "000000".equals(code)
                || "0".equals(code)
                || "success".equalsIgnoreCase(code);
    }

    private void appendDiscounts(
            List<Map<String, Object>> out, String fundCode, List<FundDiscount> list, String defaultCustType) {
        if (list == null) {
            return;
        }
        for (FundDiscount vo : list) {
            if (vo == null) {
                continue;
            }
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("fund_code", fundCode);
            if (vo.getBusCode() != null) {
                m.put("busincode", vo.getBusCode());
            }
            if (vo.getDiscount() != null) {
                m.put("discount", vo.getDiscount());
                m.put("discount_desc", describeDiscount(vo.getDiscount()));
            }
            if (vo.getFixDiscount() != null) {
                m.put("fix_discount", vo.getFixDiscount());
                m.put("fix_discount_desc", describeDiscount(vo.getFixDiscount()));
            }
            String custType = vo.getCusType() != null ? vo.getCusType() : defaultCustType;
            if (custType != null) {
                m.put("custtype", custType);
            }
            if (vo.getNetNo() != null) {
                m.put("net_no", vo.getNetNo());
            }
            if (vo.getFlag() != null) {
                m.put("flag", vo.getFlag());
            }
            out.add(m);
        }
    }

    /**
     * 折扣原始值 → 对客说明，避免模型自行解读 0/1/0.1。
     * 1=不打折；0=无费用；其他按「因子×10」折（0.1→1折）。
     */
    private static String describeDiscount(Object raw) {
        if (raw == null) {
            return "";
        }
        BigDecimal value;
        try {
            value = new BigDecimal(String.valueOf(raw).trim());
        } catch (Exception ex) {
            return "折扣值=" + raw;
        }
        if (value.compareTo(BigDecimal.ZERO) == 0) {
            return "无费用（免收）";
        }
        if (value.compareTo(BigDecimal.ONE) == 0) {
            return "不打折（按原费率）";
        }
        BigDecimal zhe = value.multiply(BigDecimal.TEN).stripTrailingZeros();
        BigDecimal factor = value.stripTrailingZeros();
        return zhe.toPlainString() + "折（按原费率×" + factor.toPlainString() + "）";
    }

    private static String mapOrderByToDateInterval(String orderBy) {
        if (orderBy == null) {
            return FundRoseReq.DATEINTERVAL_THISYEAR;
        }
        switch (orderBy) {
            case "week_rose":
                return FundRoseReq.DATEINTERVAL_WEEK;
            case "month_rose":
                return FundRoseReq.DATEINTERVAL_MONTH;
            case "year_rose":
                return FundRoseReq.DATEINTERVAL_YEAR;
            case "setup_rose":
                return FundRoseReq.DATEINTERVAL_SETUP;
            case "thisyear_rose":
            default:
                return FundRoseReq.DATEINTERVAL_THISYEAR;
        }
    }
}
