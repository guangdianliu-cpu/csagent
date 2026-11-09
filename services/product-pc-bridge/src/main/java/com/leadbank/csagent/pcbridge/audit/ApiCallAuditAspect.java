package com.leadbank.csagent.pcbridge.audit;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.aspectj.lang.reflect.MethodSignature;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.context.request.ServletRequestAttributes;

import javax.servlet.http.HttpServletRequest;
import java.lang.annotation.Annotation;
import java.lang.reflect.Method;
import java.time.Instant;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/**
 * AOP：记录 Agent/大模型经 bridge HTTP 调用的入参、出参与耗时。
 * 明细走 Logback 专用 logger（滚动 + gzip），控制台只打摘要。
 */
@Aspect
@Component
public class ApiCallAuditAspect {

    private static final Logger log = LoggerFactory.getLogger(ApiCallAuditAspect.class);
    /** 与 logback-spring.xml 中 BRIDGE_API_AUDIT 对应，输出纯 JSON 行 */
    private static final Logger AUDIT_JSON = LoggerFactory.getLogger("BRIDGE_API_AUDIT");

    private final ObjectMapper objectMapper;

    @Value("${bridge.audit.enabled:true}")
    private boolean enabled;

    @Value("${bridge.audit.max-body-chars:4000}")
    private int maxBodyChars;

    public ApiCallAuditAspect(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    @Around("execution(public * com.leadbank.csagent.pcbridge.web..*(..))")
    public Object aroundController(ProceedingJoinPoint pjp) throws Throwable {
        if (!enabled) {
            return pjp.proceed();
        }
        long startNs = System.nanoTime();
        String callId = UUID.randomUUID().toString().replace("-", "");
        MethodSignature signature = (MethodSignature) pjp.getSignature();
        Method method = signature.getMethod();
        String methodName = signature.getDeclaringType().getSimpleName() + "." + signature.getName();

        Map<String, Object> record = new LinkedHashMap<String, Object>();
        record.put("call_id", callId);
        record.put("ts", Instant.now().toString());
        record.put("method", methodName);
        fillHttpContext(record);
        record.put("args", buildArgs(method, signature.getParameterNames(), pjp.getArgs()));

        Object result = null;
        Throwable error = null;
        try {
            result = pjp.proceed();
            return result;
        } catch (Throwable ex) {
            error = ex;
            throw ex;
        } finally {
            long durationMs = (System.nanoTime() - startNs) / 1000000L;
            record.put("duration_ms", durationMs);
            if (error != null) {
                record.put("success", Boolean.FALSE);
                record.put("error", error.getClass().getSimpleName() + ": " + safeMsg(error.getMessage()));
            } else {
                record.put("success", Boolean.TRUE);
                record.put("response", truncate(toJsonNode(result)));
            }
            log.info(
                    "接口审计 callId={} method={} 耗时={}ms 成功={} path={}",
                    callId,
                    methodName,
                    durationMs,
                    error == null,
                    record.get("path")
            );
            writeAuditJson(record);
        }
    }

    private void writeAuditJson(Map<String, Object> record) {
        try {
            AUDIT_JSON.info(objectMapper.writeValueAsString(record));
        } catch (Exception ex) {
            log.warn("审计日志写入失败 error={}", ex.toString());
        }
    }

    private void fillHttpContext(Map<String, Object> record) {
        try {
            ServletRequestAttributes attrs =
                    (ServletRequestAttributes) RequestContextHolder.getRequestAttributes();
            if (attrs == null) {
                return;
            }
            HttpServletRequest request = attrs.getRequest();
            if (request == null) {
                return;
            }
            record.put("http_method", request.getMethod());
            record.put("path", request.getRequestURI());
            String query = request.getQueryString();
            if (query != null && !query.isEmpty()) {
                record.put("query", query);
            }
            // Agent 侧这一轮的 trace 与会话：与 csagent 的会话审计按 trace_id 对得上
            putHeader(record, request, "X-Csagent-Trace-Id", "agent_trace_id");
            putHeader(record, request, "X-Csagent-Session", "agent_session_id");
        } catch (Exception ignored) {
            // 非 Web 线程调用时忽略
        }
    }

    private static void putHeader(
            Map<String, Object> record, HttpServletRequest request, String header, String key) {
        String value = request.getHeader(header);
        if (value != null && !value.isEmpty()) {
            record.put(key, value.length() > 128 ? value.substring(0, 128) : value);
        }
    }

    private Map<String, Object> buildArgs(Method method, String[] names, Object[] args) {
        if (args == null || args.length == 0) {
            return Collections.emptyMap();
        }
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        Annotation[][] paramAnns = method.getParameterAnnotations();
        for (int i = 0; i < args.length; i++) {
            String name = (names != null && i < names.length && names[i] != null)
                    ? names[i]
                    : ("arg" + i);
            if (isSensitiveHeader(paramAnns, i, name)) {
                out.put(name, args[i] == null ? null : "***");
                continue;
            }
            out.put(name, truncate(toJsonNode(args[i])));
        }
        return out;
    }

    private static boolean isSensitiveHeader(Annotation[][] paramAnns, int index, String name) {
        if ("token".equalsIgnoreCase(name) || name.toLowerCase().contains("token")) {
            return true;
        }
        if (paramAnns == null || index >= paramAnns.length) {
            return false;
        }
        for (Annotation ann : paramAnns[index]) {
            if (ann instanceof RequestHeader) {
                RequestHeader header = (RequestHeader) ann;
                String value = header.value();
                if (value == null || value.isEmpty()) {
                    value = header.name();
                }
                if (value != null && value.toLowerCase().contains("token")) {
                    return true;
                }
            }
        }
        return false;
    }

    private Object toJsonNode(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof String
                || value instanceof Number
                || value instanceof Boolean) {
            return value;
        }
        try {
            return objectMapper.valueToTree(value);
        } catch (Exception ex) {
            return String.valueOf(value);
        }
    }

    private Object truncate(Object value) {
        if (value == null) {
            return null;
        }
        try {
            String json = objectMapper.writeValueAsString(value);
            if (json.length() <= maxBodyChars) {
                return value;
            }
            Map<String, Object> truncated = new LinkedHashMap<String, Object>();
            truncated.put("_truncated", Boolean.TRUE);
            truncated.put("_chars", json.length());
            truncated.put("preview", json.substring(0, maxBodyChars));
            return truncated;
        } catch (Exception ex) {
            String text = String.valueOf(value);
            if (text.length() <= maxBodyChars) {
                return text;
            }
            return text.substring(0, maxBodyChars) + "...(truncated)";
        }
    }

    private static String safeMsg(String msg) {
        if (msg == null) {
            return "";
        }
        return msg.length() > 500 ? msg.substring(0, 500) : msg;
    }
}
