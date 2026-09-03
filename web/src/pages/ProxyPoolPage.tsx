import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Loader2, Save } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";

export function ProxyPoolPage() {
  const [value, setValue] = useState("");
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api
      .getProxyPool()
      .then((result) => {
        setValue(result.value);
        setCount(result.count);
      })
      .catch((error) => {
        toast.error(error instanceof Error ? error.message : "加载代理池失败");
      })
      .finally(() => setLoading(false));
  }, []);

  async function handleSave(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const result = await api.updateProxyPool(value);
      setValue(result.value);
      setCount(result.count);
      toast.success(`已保存 ${result.count} 个代理`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存代理池失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="p-4 md:p-8">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-foreground">代理池</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          每行填写一个 HTTP 或 HTTPS 代理；账号会尽量使用不同且固定的代理。
        </p>
      </div>

      <Card className="max-w-3xl">
        <CardHeader className="pb-4">
          <CardTitle className="text-base">代理地址</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <form onSubmit={handleSave} className="space-y-4">
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <Label htmlFor="proxy-pool">每行一个代理</Label>
                  <span className="text-xs text-muted-foreground">
                    当前 {count} 个
                  </span>
                </div>
                <textarea
                  id="proxy-pool"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  placeholder={"http://user:password@host:port\nhttps://host:port"}
                  spellCheck={false}
                  autoComplete="off"
                  disabled={saving}
                  className="min-h-64 w-full resize-y rounded-md border border-input bg-background px-3 py-2 font-mono text-sm leading-6 outline-none ring-offset-background placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                />
              </div>
              <div className="flex justify-end">
                <Button type="submit" disabled={saving} className="gap-2">
                  {saving ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <Save size={14} />
                  )}
                  保存配置
                </Button>
              </div>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
