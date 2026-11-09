package com.leadbank.csagent.pcbridge.mapper;

import com.leadbank.productcenter.facade.bean.cms.AssetInvestBean;
import com.leadbank.productcenter.facade.bean.cms.FundArchiveInfoResp;
import com.leadbank.productcenter.facade.bean.cms.FundAssetConfigChangeBean;
import com.leadbank.productcenter.facade.bean.cms.FundAssetPositionResp;
import com.leadbank.productcenter.facade.bean.cms.FundDividendDistrBean;
import com.leadbank.productcenter.facade.bean.cms.FundInfoBean;
import com.leadbank.productcenter.facade.bean.cms.FundInfoResp;
import com.leadbank.productcenter.facade.bean.cms.FundManagerBean;
import com.leadbank.productcenter.facade.bean.cms.FundRankingInfo;
import com.leadbank.productcenter.facade.bean.cms.FundRose;
import com.leadbank.productcenter.facade.bean.product.FundAssetScaleBean;
import com.leadbank.productcenter.facade.bean.product.FundFareRatio;
import com.leadbank.productcenter.facade.bean.product.FundHolderInfoBean;
import com.leadbank.productcenter.facade.bean.product.FundInfoBaseDetailResp;
import com.leadbank.productcenter.facade.bean.product.FundInfoBasePageBean;
import com.leadbank.productcenter.facade.bean.product.FundInvestStyleResp;
import com.leadbank.productcenter.facade.bean.product.FundOpenPeriodTrn;
import com.leadbank.productcenter.facade.bean.product.FundRiskIndexResp;
import com.leadbank.productcenter.facade.bean.product.TradeLimitBean;
import com.leadbank.productcenter.facade.param.resp.TaInfo;
import com.leadbank.productcenter.facade.quota.QuotaConfigBean;
import com.leadbank.productcenter.facade.quota.QueryQuotaResp;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 产品中心 Facade DTO → product_api 逻辑字段。
 * 字段对齐 fims detail.app（FundInfoResp / 档案 / 持仓 / 风险等）。
 */
public final class ProductFieldMapper {

    private ProductFieldMapper() {
    }

    /** 柜台详情（FundBaseInfoFacade）— 补充字段用。 */
    public static Map<String, Object> fundDetail(FundInfoBaseDetailResp d) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (d == null) {
            return m;
        }
        put(m, "fund_code", d.getFundCode());
        put(m, "fund_name", d.getFundName());
        put(m, "fund_full_name", d.getFundFullName());
        put(m, "nav", d.getNav());
        put(m, "nav_update_date", d.getNavUpdateDate());
        put(m, "cumulative_nav", d.getCumulativeNav());
        put(m, "product_type", d.getProductType());
        put(m, "fund_category", d.getFundCategory());
        put(m, "fund_type", d.getFundType());
        put(m, "fund_sub_type", d.getFundSubType());
        putAlways(m, "consign_flag", d.getConsignFlag2());
        putAlways(m, "fundstate", d.getFundState());
        putAlways(m, "buystatus", d.getBuyStatus());
        putAlways(m, "redeemstatus", d.getRedeemStatus());
        put(m, "fund_sector", firstNonBlank(d.getFundRisk(), d.getFundSector()));
        put(m, "manager_code", d.getManagerCode());
        put(m, "manager_name", d.getManagerName());
        put(m, "tacode", d.getTaCode());
        put(m, "taname", d.getTaName());
        put(m, "setup_date", d.getfInfoSetupDate());
        put(m, "lock_period", d.getLockPeriod());
        put(m, "lock_period_unit", d.getLockPeriodUnit());
        put(m, "dividend_method", d.getDefDividendMethod());
        put(m, "issue_size", d.getIssueSize());
        return m;
    }

    /**
     * fims detail.app HEADER 主数据源：FundInfoFacade.queryFundInfo。
     */
    public static Map<String, Object> fundDetailFromCms(FundInfoResp d) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (d == null) {
            return m;
        }
        put(m, "fund_code", d.getFundcode());
        put(m, "fund_name", d.getFundname());
        put(m, "fund_full_name", d.getFundFullName());
        put(m, "fund_type", d.getFundtype());
        put(m, "fund_type_name", d.getFundtypename());
        put(m, "fund_sub_type", d.getFundSubType());
        put(m, "fund_sub_type_name", d.getFundSubTypeName());
        put(m, "fundstate", d.getFundstate());
        putAlways(m, "consign_flag", d.getConsignflag2());
        put(m, "dividend_method", d.getDividendmethod());
        put(m, "nav", d.getNav());
        put(m, "nav_display", d.getNavDisplay());
        put(m, "nav_update_date", d.getNavdate());
        put(m, "cumulative_nav", firstNonNull(d.getAccuUnitNav(), d.getTotalnav()));
        put(m, "total_nav", d.getTotalnav());
        putAlways(m, "buystatus", d.getBuystatus());
        putAlways(m, "redeemstatus", d.getRedeemstatus());
        put(m, "day_rose", d.getRose());
        put(m, "unit_yield", d.getUnityield());
        put(m, "yearly_roe", d.getYearlyroe());
        put(m, "week_rose", d.getWeekrose());
        put(m, "month_rose", d.getMonthrose());
        put(m, "quarter_rose", d.getQuarterrose());
        put(m, "halfyear_rose", d.getHalfyearrose());
        put(m, "year_rose", d.getYearrose());
        put(m, "thisyear_rose", d.getThisyearrose());
        put(m, "twoyear_rose", d.getTwoyearrose());
        put(m, "setup_rose", d.getSetuprose());
        put(m, "fund_sector", firstNonBlank(d.getRiskyType(), d.getFundSector()));
        put(m, "risky_type", d.getRiskyType());
        put(m, "risky_name", d.getRiskyName());
        put(m, "fund_category", d.getFundCategory());
        put(m, "product_type", d.getProductType());
        put(m, "new_fund_type", d.getNewFundType());
        put(m, "three_fund_type", d.getThreeFundType());
        put(m, "comp_id", d.getCompId());
        put(m, "manager_code", d.getCompId());
        put(m, "manager_name", firstNonBlank(d.getCompName(), d.getFundAdmin()));
        put(m, "fund_admin", d.getFundAdmin());
        put(m, "fund_trustee", d.getFundTrustee());
        put(m, "fund_managers", d.getFundManagers());
        put(m, "tacode", d.getTaCode());
        put(m, "taname", d.getTaName());
        put(m, "setup_date", d.getSetupDate());
        put(m, "market_date", d.getMarketDate());
        put(m, "issue_size", d.getIssueSize());
        put(m, "asset_amount", d.getAssetAmount());
        put(m, "asset_report_date", d.getAssetReportDate());
        put(m, "min_value", d.getMinValue());
        put(m, "min_value_display", d.getMinValueDisplay());
        put(m, "max_value", d.getMaxValue());
        put(m, "max_value_display", d.getMaxValueDisplay());
        put(m, "day_max_sum", d.getDaymaxsum());
        put(m, "day_max_sum_display", d.getDaymaxsumDisplay());
        put(m, "mini_hold_amount", d.getMiniHoldAmount());
        put(m, "lock_period", d.getLockPeriod() > 0 ? d.getLockPeriod() : null);
        put(m, "lock_period_unit", d.getLockPeriodUnit());
        put(m, "lock_period_desc", lockPeriodDesc(d.getLockPeriod(), d.getLockPeriodUnit()));
        put(m, "closed_flag", d.getClosedFlag());
        put(m, "closed_period", d.getClosedPeriod());
        put(m, "closed_period_unit", d.getClosedPeriodUnit());
        put(m, "app_ipo_flag", d.getAppIpoFlag());
        put(m, "sell_out_flag", d.getSellOutFlag());
        put(m, "public_flag", d.getPublicFlag());
        put(m, "purchase_start_dt", d.getPurchaseStartDt());
        put(m, "purchase_end_dt", d.getPurchaseEndDt());
        put(m, "redeem_start_dt", d.getRedeemStartDt());
        put(m, "redeem_end_dt", d.getRedeemEndDt());
        put(m, "sele_start_time", d.getSeleStartTime());
        put(m, "sele_end_time", d.getSeleEndTime());
        put(m, "buy_ack_days", d.getBuyAckDays());
        put(m, "redeem_ack_days", d.getRedeemAckDays());
        put(m, "sub_ack_days", d.getSubAckDays());
        put(m, "manage_ratio", d.getManageRatio());
        put(m, "hosting_rate", d.getHostingRate());
        put(m, "service_rate", d.getServiceRate());
        put(m, "admin_service_rate", d.getAdminServiceRate());
        put(m, "open_regulation", d.getOpenRegulation());
        put(m, "redem_regulation", d.getRedemRegulation());
        put(m, "profit_explain", d.getProfitExplain());
        put(m, "invest_object", d.getInvestobject());
        put(m, "fund_desc", d.getFundDesc());
        put(m, "fund_intro", d.getFundIntro());
        put(m, "nav_update_desc", d.getNavUpdateDesc());
        put(m, "operation_period", d.getOperationPeriod());
        put(m, "period_type", d.getPeriodType());
        put(m, "trade_fund_code", d.getTradeFundCode());
        // 空则调用方可用当前 fund_code 回退
        put(m, "fund_main_code", firstNonBlank(d.getFundMainCode(), d.getFundcode()));
        put(m, "is_auto_invest", d.getIsAutoInvest());
        put(m, "net_sale_flag", d.getNetSaleFlag());
        put(m, "same_day_purchase_flag", d.getSameDayPurchaseFlag());
        put(m, "soon_purchase_flag", d.getSoonPurchaseFlag());
        put(m, "only_appt_pur_flag", d.getOnlyApptPurFlag());
        put(m, "result_reward_standard", d.getResultRewardStandard());
        put(m, "result_reward_remark", d.getResultRewardRemark());
        put(m, "float_fee_type", d.getFloatFeeType());
        put(m, "float_fee", d.getFloatFee());
        put(m, "invest_cust_count", d.getInvestCustCount());
        put(m, "invest_amount_count", d.getInvestAmountCount());
        put(m, "usable_invest_cust_count", d.getUsableInvestCustCount());
        put(m, "usable_invest_amount_count", d.getUsableInvestAmountCount());
        put(m, "fund_prod_file", d.getFundProdFile());
        put(m, "settle_method", d.getSettleMethod());
        put(m, "settle_method_name", d.getSettltMethodName());
        put(m, "redemption_cash_date", d.getRedemptionCashDate());
        return m;
    }

    /** OnlineFacade.queryFundByMainFund 返回的同系列从基金份额摘要 */
    public static Map<String, Object> fromMainFundBean(FundInfoBean b) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (b == null) {
            return m;
        }
        put(m, "fund_code", b.getFundcode());
        put(m, "fund_name", b.getFundname());
        put(m, "fund_type", b.getFundtype());
        put(m, "fund_type_name", b.getFundtypename());
        put(m, "out_fund_code", b.getOutFundCode());
        put(m, "trade_fund_code", b.getTradeFundCode());
        put(m, "buystatus", b.getBuystatus());
        put(m, "redeemstatus", b.getRedeemstatus());
        return m;
    }

    public static Map<String, Object> archive(FundArchiveInfoResp a) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (a == null) {
            return m;
        }
        put(m, "fund_code", a.getFundcode());
        put(m, "fund_name", a.getFundname());
        put(m, "fund_type", a.getFundtype());
        put(m, "fund_type_name", a.getFundtypename());
        put(m, "establish_date", a.getEstablishdate());
        put(m, "asset_amount", a.getAssetamount());
        put(m, "share", a.getShare());
        put(m, "pre_asset_amount", a.getPreassetamount());
        put(m, "report_date", a.getReportdate());
        put(m, "fund_admin", a.getFundadmin());
        put(m, "fund_trustee", a.getFundtrustee());
        put(m, "fund_managers", a.getFundmanagers());
        put(m, "invest_field", a.getInvestField());
        put(m, "invest_object", a.getInvestobject());
        put(m, "invest_strategy", a.getInveststrategy());
        put(m, "risk_return_character", a.getRiskReturnCharacter());
        put(m, "performance_benchmark", a.getPerformanceBenchmark());
        put(m, "default_dividend_type", a.getDefaultdividendtype());
        put(m, "collect_fees_type", a.getCollectfeestype());
        put(m, "lock_period", a.getLockPeriod() > 0 ? a.getLockPeriod() : null);
        put(m, "lock_period_unit", a.getLockPeriodUnit());
        put(m, "is_pi_fof", a.getIsPiFof());
        if (a.getManagerlist() != null && !a.getManagerlist().isEmpty()) {
            List<Map<String, Object>> managers = new ArrayList<>();
            for (FundManagerBean mb : a.getManagerlist()) {
                if (mb == null) {
                    continue;
                }
                Map<String, Object> row = new LinkedHashMap<>();
                put(row, "manager_name", mb.getManagername());
                put(row, "assume_date", mb.getAssumedate());
                put(row, "start_date", mb.getStartdate());
                put(row, "end_date", mb.getEnddate());
                put(row, "post_status", mb.getPoststatus());
                put(row, "company", mb.getCompany());
                put(row, "education", mb.getEducation());
                put(row, "resume", mb.getResume());
                managers.add(row);
            }
            if (!managers.isEmpty()) {
                m.put("managers", managers);
            }
        }
        return m;
    }

    public static Map<String, Object> riskIndex(FundRiskIndexResp r) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (r == null) {
            return m;
        }
        put(m, "fund_code", r.getFundCode());
        put(m, "index_type", r.getIndexType());
        put(m, "trade_date", r.getTradeDate());
        put(m, "std_halfyear", r.getStdarddevHalfyear());
        put(m, "std_year", r.getStdarddevYear());
        put(m, "std_twoyear", r.getStdarddevTwoyear());
        put(m, "std_threeyear", r.getStdarddevThreeyear());
        put(m, "std_fiveyear", r.getStdarddevFiveyear());
        put(m, "std_since_found", r.getStdarddevSincefound());
        put(m, "sharpe_halfyear", r.getSharpratioHalfyear());
        put(m, "sharpe_year", r.getSharpratioYear());
        put(m, "sharpe_twoyear", r.getSharpratioTwoyear());
        put(m, "sharpe_threeyear", r.getSharpratioThreeyear());
        return m;
    }

    public static Map<String, Object> holdings(FundAssetPositionResp p) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (p == null) {
            return m;
        }
        put(m, "fund_code", p.getFundcode());
        m.put("stock_holdings", mapInvestList(p.getStockinvestlist()));
        m.put("bond_holdings", mapInvestList(p.getDebtinvestlist()));
        // detail.app INVEST 资产配置序列（股票/债/现金/其他占净值%）
        m.put("asset_config_changes", mapAssetConfigChanges(p.getFundAssetConfigChangeBeans()));
        return m;
    }

    public static List<Map<String, Object>> mapAssetConfigChanges(List<FundAssetConfigChangeBean> list) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (list == null) {
            return out;
        }
        for (FundAssetConfigChangeBean b : list) {
            if (b == null) {
                continue;
            }
            Map<String, Object> m = new LinkedHashMap<>();
            put(m, "report_date", b.getReportdate());
            put(m, "stock_percent", b.getStockpercent());
            put(m, "bond_percent", b.getDebtpercent());
            put(m, "cash_percent", b.getCashpercent());
            put(m, "other_percent", b.getOtherpercent());
            out.add(m);
        }
        return out;
    }

    public static List<Map<String, Object>> assetScaleRows(String fundCode, List<FundAssetScaleBean> list) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (list == null) {
            return out;
        }
        for (FundAssetScaleBean b : list) {
            if (b == null) {
                continue;
            }
            Map<String, Object> m = new LinkedHashMap<>();
            put(m, "fund_code", firstNonBlank(b.getFundCode(), fundCode));
            put(m, "change_date", b.getChangeDate());
            put(m, "ann_date", b.getAnnDate());
            put(m, "fund_total_share", b.getFundTotalShare());
            put(m, "fund_total_asset", b.getFundTotalAsset());
            out.add(m);
        }
        return out;
    }

    public static List<Map<String, Object>> holderRows(String fundCode, List<FundHolderInfoBean> list) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (list == null) {
            return out;
        }
        for (FundHolderInfoBean b : list) {
            if (b == null) {
                continue;
            }
            Map<String, Object> m = new LinkedHashMap<>();
            put(m, "fund_code", firstNonBlank(b.getFundCode(), fundCode));
            put(m, "ann_date", b.getAnnDate());
            put(m, "end_date", b.getEndDate());
            put(m, "holder_number", b.getHolderNumber());
            put(m, "institution_holding_pct", b.getInstitutionHoldingPct());
            put(m, "personal_holding_pct", b.getPersonalHoldingPct());
            put(m, "mngemp_holding_pct", b.getMngempHoldingPct());
            put(m, "institution_holding_share", b.getInstitutionHoldingShare());
            put(m, "personal_holding_share", b.getPersonalHoldingShare());
            put(m, "holding_total_share", b.getHoldingTotalShare());
            out.add(m);
        }
        return out;
    }

    public static List<Map<String, Object>> dividendRows(String fundCode, List<FundDividendDistrBean> list) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (list == null) {
            return out;
        }
        for (FundDividendDistrBean b : list) {
            if (b == null) {
                continue;
            }
            Map<String, Object> m = new LinkedHashMap<>();
            put(m, "fund_code", firstNonBlank(b.getFundcode(), fundCode));
            put(m, "fund_name", b.getFundname());
            put(m, "report_date", b.getReportDate());
            put(m, "dividend_year", b.getDividendyear());
            put(m, "register_date", b.getRegisterdate());
            put(m, "ex_dividend_date", b.getExdividenddate());
            put(m, "every_dividend", b.getEverydividend());
            put(m, "dividend_date", b.getDividenddate());
            out.add(m);
        }
        return out;
    }

    /** 基金名称/代码模糊检索摘要行（fundBaseInfoPage）。 */
    public static Map<String, Object> fundSearchRow(FundInfoBasePageBean b) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (b == null) {
            return m;
        }
        put(m, "fund_code", b.getFundCode());
        put(m, "fund_name", b.getFundName());
        put(m, "fund_type", b.getFundType());
        put(m, "fund_type_name", b.getFundTypeFormat());
        put(m, "fund_sub_type", b.getFundSubType());
        put(m, "fund_sub_type_name", b.getFundSubTypeFormat());
        put(m, "product_type", b.getProductType());
        putAlways(m, "fundstate", b.getFundState());
        putAlways(m, "consign_flag", b.getConsignFlag2());
        put(m, "fund_sector", b.getFundRisk());
        put(m, "nav", b.getNav());
        put(m, "nav_update_date", b.getNavDate());
        put(m, "manager_code", b.getManagerCode());
        put(m, "manager_name", b.getManagerName());
        put(m, "tacode", b.getTaCode());
        put(m, "taname", b.getTaName());
        return m;
    }

    public static Map<String, Object> managerOrgRow(
            com.leadbank.productcenter.facade.bean.product.FundManagerBean b) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (b == null) {
            return m;
        }
        put(m, "manager_code", b.getManagerCode());
        put(m, "manager_name", b.getManagerName());
        return m;
    }

    public static Map<String, Object> taInfo(TaInfo info) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (info == null) {
            return m;
        }
        put(m, "tacode", info.getTaCode());
        put(m, "taname", info.getTaName());
        put(m, "property", info.getProperty());
        put(m, "append_dimension", info.getAppendDimension());
        put(m, "ta_credit_code", info.getTaCreditCode());
        put(m, "is_support_t05_advance", info.getIsSupportT05Advance());
        return m;
    }

    public static Map<String, Object> investStyle(FundInvestStyleResp r) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (r == null) {
            return m;
        }
        put(m, "fund_code", r.getFundCode());
        put(m, "fund_style_sector", r.getFundStyleSector());
        put(m, "fund_style_sector_name", r.getFundStyleSectorName());
        put(m, "fund_sector_entry_dt", r.getFundSectorentryDt());
        put(m, "fund_sector_exit_dt", r.getFundSectorexitDt());
        put(m, "report_date", r.getReportDate());
        put(m, "scale_style", r.getScaleStyle());
        put(m, "value_style", r.getValueStyle());
        return m;
    }

    public static List<Map<String, Object>> feeRates(
            String fundCode, List<FundFareRatio> list, String faretype, String custType) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (list == null) {
            return out;
        }
        for (FundFareRatio r : list) {
            if (r == null) {
                continue;
            }
            Map<String, Object> m = new LinkedHashMap<>();
            put(m, "fund_code", fundCode);
            put(m, "faretype", faretype);
            put(m, "custtype", custType);
            // 单笔档 farevalue 可能为 0；fareratio/farevalue 始终写出便于 Agent 区分「费率%」与「元/笔」
            m.put("fareratio", r.getFareRatio());
            m.put("farevalue", r.getFareValue());
            put(m, "minvalue", r.getMinvalue());
            put(m, "maxvalue", r.getMaxvalue());
            put(m, "unit", r.getUnit());
            put(m, "spec_redeem", r.getSpecRedeem());
            put(m, "spec_redeem_flag", r.getSpecRedeemFlag());
            put(m, "flag", r.getFlag());
            out.add(m);
        }
        return out;
    }

    public static Map<String, Object> tradeLimit(TradeLimitBean b) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (b == null) {
            return m;
        }
        put(m, "fund_code", b.getFundCode());
        put(m, "busincode", b.getBusinessCode());
        put(m, "sharetype", b.getShareType());
        put(m, "custtype", b.getCusType());
        put(m, "trusttype", b.getTrustType());
        put(m, "capital_mode", b.getCapitalMode());
        put(m, "trade_flag", b.getTradeFlag());
        put(m, "minvalue", b.getMinvalue());
        put(m, "maxvalue", b.getMaxvalue());
        put(m, "second_min", b.getSecondMin());
        put(m, "second_min_buy_ind", b.getSecondMinBuyInd());
        put(m, "min_share", b.getMinShare());
        put(m, "custmaxvalue", b.getCusMaxValue());
        put(m, "day_max_sum", b.getDayMaxSum());
        put(m, "range", b.getRange());
        return m;
    }

    public static Map<String, Object> openPeriod(FundOpenPeriodTrn p) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (p == null) {
            return m;
        }
        put(m, "fund_code", p.getFundCode());
        put(m, "busincode", p.getBusinessCode());
        put(m, "trade_start_date", p.getTradeStartDate());
        put(m, "trade_end_date", p.getTradeEndDate());
        put(m, "appt_start_time", p.getAppStartTime());
        put(m, "appt_end_time", p.getAppEndTime());
        return m;
    }

    public static Map<String, Object> performanceFromRanking(String fundCode, List<FundRankingInfo> list) {
        Map<String, Object> m = new LinkedHashMap<>();
        put(m, "fund_code", fundCode);
        if (list == null) {
            return m;
        }
        List<Map<String, Object>> periods = new ArrayList<>();
        for (FundRankingInfo info : list) {
            if (info == null || info.getRosedatetype() == null) {
                continue;
            }
            String type = info.getRosedatetype();
            BigDecimal rose = info.getRose();
            BigDecimal ranking = info.getRanking();
            Map<String, Object> period = new LinkedHashMap<>();
            put(period, "rose_date_type", type);
            put(period, "rose_date_type_name", info.getRosedatetypename());
            put(period, "rose", rose);
            put(period, "ranking", ranking);
            put(period, "total_fund_num", info.getTotalfundnum());
            put(period, "yearly_roe", info.getYearlyroe());
            put(period, "workday", info.getRoserankdate());
            periods.add(period);

            if ("01".equals(type)) {
                put(m, "thisyear_rose", rose);
                put(m, "thisyear_ranking", ranking);
                put(m, "workday", info.getRoserankdate());
            } else if ("02".equals(type)) {
                put(m, "week_rose", rose);
                put(m, "week_ranking", ranking);
            } else if ("03".equals(type)) {
                put(m, "month_rose", rose);
                put(m, "month_ranking", ranking);
            } else if ("04".equals(type)) {
                put(m, "quarter_rose", rose);
                put(m, "quarter_ranking", ranking);
            } else if ("05".equals(type)) {
                put(m, "halfyear_rose", rose);
                put(m, "halfyear_ranking", ranking);
            } else if ("06".equals(type)) {
                put(m, "year_rose", rose);
                put(m, "year_ranking", ranking);
            } else if ("07".equals(type)) {
                put(m, "twoyear_rose", rose);
                put(m, "twoyear_ranking", ranking);
            } else if ("11".equals(type)) {
                put(m, "setup_rose", rose);
                put(m, "setup_ranking", ranking);
            }
        }
        if (!periods.isEmpty()) {
            m.put("periods", periods);
        }
        return m;
    }

    public static Map<String, Object> roseRow(FundRose rose, String orderByField, String fundType) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (rose == null) {
            return m;
        }
        put(m, "fund_code", rose.getFundcode());
        put(m, "fund_name", rose.getFundname());
        put(m, "fund_type", fundType);
        put(m, "nav", rose.getNownav());
        put(m, "workday", rose.getNavday());
        put(m, "buystatus", rose.getBuystatus());
        put(m, orderByField, rose.getRose());
        return m;
    }

    public static Map<String, Object> quotaInfo(QueryQuotaResp q) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (q == null) {
            return m;
        }
        put(m, "fund_code", q.getFundCode());
        put(m, "fund_name", q.getFundName());
        put(m, "fund_category", q.getFundCategory());
        put(m, "fund_type", q.getFundType());
        put(m, "quota_type", q.getQuotaType());
        put(m, "quota_limit", q.getQuotaLimit());
        put(m, "amount_limit", q.getAmountLimit());
        put(m, "total_quota", q.getTotalQuota());
        put(m, "total_amount", q.getTotalAmount());
        put(m, "share_quota", q.getShareQuota());
        put(m, "share_amount", q.getShareAmount());
        put(m, "share_usable_quota", q.getShareUsableQuota());
        put(m, "share_usable_amount", q.getShareUsableAmount());
        put(m, "excl_quota", q.getExclQuota());
        put(m, "excl_amount", q.getExclAmount());
        put(m, "excl_usable_quota", q.getExclUsableQuota());
        put(m, "excl_usable_amount", q.getExclUsableAmount());
        put(m, "purchase_cfg_type", q.getPurchaseCfgType());
        put(m, "subscribe_cfg_type", q.getSubscribeCfgType());
        put(m, "quota_amount_flag", q.getQuotaAmountFlag());
        put(m, "only_appt_pur_flag", q.getOnlyApptPurFlag());
        return m;
    }

    /** 网点专享额度明细（queryQuotaConfig → QuotaConfigBean）。 */
    public static List<Map<String, Object>> quotaControlRows(
            String fundCode, List<QuotaConfigBean> list) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (list == null) {
            return out;
        }
        for (QuotaConfigBean b : list) {
            if (b == null) {
                continue;
            }
            Map<String, Object> m = new LinkedHashMap<>();
            put(m, "fund_code", firstNonBlank(b.getFundCode(), fundCode));
            put(m, "fund_name", b.getFundName());
            put(m, "net_no", b.getNetNo());
            put(m, "net_name", b.getNetName());
            put(m, "share_quota", b.getUsableExclCustQuota());
            put(m, "excl_cust_quota", b.getExclCustQuota());
            put(m, "excl_amount", b.getExclAmount());
            put(m, "usable_excl_amount", b.getUsableExclAmount());
            put(m, "freeze_quota", b.getFreezeQuota());
            put(m, "quota_type", b.getQuotaType());
            out.add(m);
        }
        return out;
    }

    private static List<Map<String, Object>> mapInvestList(List<AssetInvestBean> list) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (list == null) {
            return out;
        }
        for (AssetInvestBean b : list) {
            if (b == null) {
                continue;
            }
            Map<String, Object> m = new LinkedHashMap<>();
            put(m, "asset_type", b.getAssettype());
            put(m, "asset_name", b.getAssetname());
            put(m, "asset_fund_code", b.getAssetfundcode());
            put(m, "net_percent", b.getNetpercent());
            put(m, "hold_shares_amount", b.getHoldsharesamount());
            put(m, "position_market_val", b.getPositionmarketval());
            put(m, "invest_year", b.getInvestyear() > 0 ? b.getInvestyear() : null);
            put(m, "invest_quarter", b.getInvestquarter());
            put(m, "day_rose", b.getDayrose());
            out.add(m);
        }
        return out;
    }

    private static String lockPeriodDesc(int period, String unit) {
        if (period <= 0) {
            return null;
        }
        String u = unit == null ? "" : unit.trim();
        if ("1".equals(u) || "D".equalsIgnoreCase(u) || "日".equals(u)) {
            return period + "日";
        }
        if ("2".equals(u) || "M".equalsIgnoreCase(u) || "月".equals(u)) {
            return period + "月";
        }
        if ("3".equals(u) || "Y".equalsIgnoreCase(u) || "年".equals(u)) {
            return period + "年";
        }
        return period + (u.isEmpty() ? "" : u);
    }

    private static void put(Map<String, Object> m, String key, Object val) {
        if (val != null) {
            m.put(key, val);
        }
    }

    /** 关键状态字段：空也写出，避免下游缺键被模型误判为「已代销/可买」 */
    private static void putAlways(Map<String, Object> m, String key, Object val) {
        if (val == null) {
            m.put(key, "");
        } else if (val instanceof String && ((String) val).trim().isEmpty()) {
            m.put(key, "");
        } else {
            m.put(key, val);
        }
    }

    private static Object firstNonNull(Object a, Object b) {
        return a != null ? a : b;
    }

    private static String firstNonBlank(String a, String b) {
        if (a != null && !a.trim().isEmpty()) {
            return a;
        }
        if (b != null && !b.trim().isEmpty()) {
            return b;
        }
        return a != null ? a : b;
    }
}
