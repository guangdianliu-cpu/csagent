package com.leadbank.csagent.pcbridge.web.dto;

import com.fasterxml.jackson.annotation.JsonAnyGetter;
import com.fasterxml.jackson.annotation.JsonInclude;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 基金详情 HTTP 响应对象。
 * <p>
 * 用显式对象 + {@code @JsonAnyGetter} 输出，调试时可稳定看到 Key
 * （含 {@code buy_ack_days} / {@code redemption_cash_date} 等，空也保留）。
 */
@JsonInclude(JsonInclude.Include.ALWAYS)
public class FundDetailResponse {

    /** 调试友好：关键交收/确认字段即使为空也出现在 JSON 中 */
    private static final String[] ENSURE_KEYS = {
            "fund_code",
            "custtype",
            "custtype_name",
            "consign_flag",
            "buystatus",
            "redeemstatus",
            "buy_ack_days",
            "redeem_ack_days",
            "sub_ack_days",
            "redemption_cash_date",
            "trade_by_custtype",
    };

    private final LinkedHashMap<String, Object> fields = new LinkedHashMap<>();

    public static FundDetailResponse from(Map<String, Object> source) {
        FundDetailResponse resp = new FundDetailResponse();
        if (source != null) {
            resp.fields.putAll(source);
        }
        for (String key : ENSURE_KEYS) {
            resp.fields.putIfAbsent(key, null);
        }
        return resp;
    }

    @JsonAnyGetter
    public Map<String, Object> getFields() {
        return fields;
    }
}
