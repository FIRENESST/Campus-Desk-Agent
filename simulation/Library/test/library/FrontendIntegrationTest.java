package library;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;

public final class FrontendIntegrationTest {
    public static void main(String[] args) throws Exception {
        HttpClient client = HttpClient.newHttpClient();
        try (LibraryServer server = new LibraryServer("127.0.0.1", 0, Fixtures.store(), Path.of("web"))) {
            server.start();
            String baseUrl = "http://127.0.0.1:" + server.port();
            HttpResponse<String> page = get(client, baseUrl + "/");
            assertAsset(page, 200, "text/html");
            String html = page.body();
            require(html.contains("lang=\"zh-CN\""), "页面必须使用中文语言标记");
            require(html.contains("馆藏") && html.contains("借阅") && html.contains("归还"), "页面必须包含完整管理界面");
            require(!html.contains("<img") && !html.contains("<iframe"), "页面不应依赖图片或外部框架页面");
            require(html.contains("styles.css") && html.contains("app.js"), "页面应加载本地 CSS 和 JS");

            HttpResponse<String> css = get(client, baseUrl + "/styles.css");
            assertAsset(css, 200, "text/css");
            require(css.body().contains("@media") && css.body().contains("serif"), "CSS 应响应式并使用衬线标题体系");

            HttpResponse<String> javascript = get(client, baseUrl + "/app.js");
            assertAsset(javascript, 200, "javascript");
            String js = javascript.body();
            require(js.contains("fetch") && js.contains("/api/books") && js.contains("/api/loans"), "JS 必须读取真实 REST 数据");
            require(js.contains("POST") && js.contains("DELETE"), "JS 必须支持借阅和归还 REST 写操作");
        }
        System.out.println("FrontendIntegrationTest: PASS");
    }

    private static HttpResponse<String> get(HttpClient client, String url) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create(url)).GET().build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static void assertAsset(HttpResponse<String> response, int status, String contentType) {
        if (response.statusCode() != status) {
            throw new AssertionError("静态资源状态应为 " + status + ": " + response.body());
        }
        String actual = response.headers().firstValue("Content-Type").orElse("");
        if (!actual.contains(contentType) || !actual.contains("utf-8")) {
            throw new AssertionError("静态资源 Content-Type 不正确: " + actual);
        }
    }

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }
}
