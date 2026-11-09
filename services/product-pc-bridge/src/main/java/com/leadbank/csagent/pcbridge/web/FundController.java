package com.leadbank.csagent.pcbridge.web;

import com.leadbank.csagent.pcbridge.service.ProductQueryService;
import com.leadbank.csagent.pcbridge.web.dto.FundDetailResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * 只读 HTTP API，字段名对齐 cs-agent Catalog 逻辑字段。
 */
@RestController
@RequestMapping("/api")
public class FundController {

    private final ProductQueryService productQueryService;

    @Value("${bridge.token:}")
    private String bridgeToken;

    public FundController(ProductQueryService productQueryService) {
        this.productQueryService = productQueryService;
    }

    private void checkToken(@RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        if (bridgeToken == null || bridgeToken.isEmpty()) {
            return;
        }
        if (token == null || !bridgeToken.equals(token)) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "invalid bridge token");
        }
    }

    @GetMapping("/health")
    public Map<String, Object> health() {
        return productQueryService.health();
    }

    /** 基金名称/代码模糊检索（FundBaseInfoFacade.fundBaseInfoPage），可附带列表筛选 */
    @GetMapping("/funds")
    public List<Map<String, Object>> searchFunds(
            @RequestParam(value = "q", required = false) String q,
            @RequestParam(value = "limit", defaultValue = "10") int limit,
            @RequestParam(value = "manager_code", required = false) String managerCode,
            @RequestParam(value = "tacode", required = false) String tacode,
            @RequestParam(value = "consign_flag", required = false) String consignFlag,
            @RequestParam(value = "fund_type", required = false) String fundType,
            @RequestParam(value = "fundstate", required = false) String fundstate,
            @RequestParam(value = "product_type", required = false) String productType,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.searchFunds(
                q, limit, managerCode, tacode, consignFlag, fundType, fundstate, productType);
    }

    /** 同主基金从基金份额列表：OnlineFacade.queryFundByMainFund */
    @GetMapping("/funds/by-main/{mainCode}")
    public List<Map<String, Object>> fundsByMain(
            @PathVariable("mainCode") String mainCode,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.listByMainFundCode(mainCode);
    }

    /**
     * 基金详情（对齐 fims detail HEADER）。
     * <p>custtype 默认 0=机构；明确问个人时传 1。返回对象便于调试看到全部 Key。
     */
    @GetMapping("/funds/{code}")
    @ResponseBody
    public FundDetailResponse fundDetail(
            @PathVariable("code") String code,
            @RequestParam(value = "custtype", defaultValue = "0") String custtype,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return FundDetailResponse.from(productQueryService.fundDetail(code, custtype));
    }

    @GetMapping("/funds/{code}/archive")
    public Map<String, Object> fundArchive(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.fundArchive(code);
    }

    @GetMapping("/funds/{code}/risk-index")
    public Map<String, Object> fundRiskIndex(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.fundRiskIndex(code);
    }

    @GetMapping("/funds/{code}/holdings")
    public Map<String, Object> fundHoldings(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.fundHoldings(code);
    }

    @GetMapping("/funds/{code}/asset-scale")
    public List<Map<String, Object>> fundAssetScale(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.fundAssetScale(code);
    }

    @GetMapping("/funds/{code}/holders")
    public List<Map<String, Object>> fundHolders(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.fundHolders(code);
    }

    @GetMapping("/funds/{code}/dividends")
    public List<Map<String, Object>> fundDividends(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.fundDividends(code);
    }

    @GetMapping("/funds/{code}/invest-style")
    public Map<String, Object> fundInvestStyle(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.fundInvestStyle(code);
    }

    @GetMapping("/funds/{code}/fees")
    public List<Map<String, Object>> fees(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.feeRates(code);
    }

    /** 多基金费率：?codes=023634,022040 */
    @GetMapping("/funds/batch/fees")
    public List<Map<String, Object>> feesBatch(
            @RequestParam("codes") String codes,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.feeRatesBatch(splitCodes(codes));
    }

    @GetMapping("/funds/{code}/trade-limits")
    public List<Map<String, Object>> tradeLimits(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.tradeLimits(code);
    }

    @GetMapping("/funds/batch/trade-limits")
    public List<Map<String, Object>> tradeLimitsBatch(
            @RequestParam("codes") String codes,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.tradeLimitsBatch(splitCodes(codes));
    }

    @GetMapping("/funds/{code}/discounts")
    public List<Map<String, Object>> discounts(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.discounts(code);
    }

    @GetMapping("/funds/batch/discounts")
    public List<Map<String, Object>> discountsBatch(
            @RequestParam("codes") String codes,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.discountsBatch(splitCodes(codes));
    }

    @GetMapping("/funds/{code}/open-periods")
    public List<Map<String, Object>> openPeriods(
            @PathVariable("code") String code,
            @RequestParam(value = "busincode", required = false) String busincode,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.openPeriods(code, busincode);
    }

    @GetMapping("/funds/{code}/performance")
    public Map<String, Object> performance(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.performance(code);
    }

    @GetMapping("/funds/ranking")
    public List<Map<String, Object>> ranking(
            @RequestParam(value = "order_by", defaultValue = "thisyear_rose") String orderBy,
            @RequestParam(value = "direction", defaultValue = "desc") String direction,
            @RequestParam(value = "limit", defaultValue = "10") int limit,
            @RequestParam(value = "fund_type", required = false) String fundType,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.ranking(orderBy, direction, limit, fundType);
    }

    @GetMapping("/quota/{code}")
    public Map<String, Object> quotaInfo(
            @PathVariable("code") String code,
            @RequestParam(value = "net_no", required = false) String netNo,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.quotaInfo(code, netNo);
    }

    @GetMapping("/quota/{code}/controls")
    public List<Map<String, Object>> quotaControls(
            @PathVariable("code") String code,
            @RequestParam(value = "net_no", required = false) String netNo,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.quotaControls(code, netNo);
    }

    @GetMapping("/workday/current")
    public Map<String, Object> currentWorkday(
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.currentWorkday();
    }

    /** 区间工作日列表 */
    @GetMapping("/workday/list")
    public List<Map<String, Object>> workdayList(
            @RequestParam("start") String start,
            @RequestParam("end") String end,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.workdayList(start, end);
    }

    /** 判断指定日期是否工作日 */
    @GetMapping("/workday/check")
    public Map<String, Object> checkWorkday(
            @RequestParam("date") String date,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.checkWorkday(date);
    }

    /** TA 名称/代码模糊检索 */
    @GetMapping("/ta")
    public List<Map<String, Object>> searchTa(
            @RequestParam(value = "q") String q,
            @RequestParam(value = "limit", defaultValue = "10") int limit,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.searchTa(q, limit);
    }

    @GetMapping("/ta/{code}")
    public Map<String, Object> taInfo(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.taInfo(code);
    }

    /** 管理人名称/代码模糊检索 */
    @GetMapping("/managers")
    public List<Map<String, Object>> searchManagers(
            @RequestParam(value = "q") String q,
            @RequestParam(value = "limit", defaultValue = "10") int limit,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.searchManagers(q, limit);
    }

    @GetMapping("/managers/{code}")
    public Map<String, Object> managerOrg(
            @PathVariable("code") String code,
            @RequestHeader(value = "X-Bridge-Token", required = false) String token) {
        checkToken(token);
        return productQueryService.managerOrg(code);
    }

    private static List<String> splitCodes(String codes) {
        if (codes == null || codes.trim().isEmpty()) {
            return Collections.emptyList();
        }
        List<String> out = new ArrayList<>();
        for (String part : codes.split("[,，\\s]+")) {
            String code = part.trim();
            if (!code.isEmpty()) {
                out.add(code);
            }
        }
        return out;
    }
}
