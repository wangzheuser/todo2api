import { useEffect, useMemo, useState } from "react";
import { Boxes, RefreshCw, Search } from "lucide-react";
import { toast } from "sonner";
import { api, ApiError } from "@/api/client";
import type { CatalogModel, ModelCatalogResponse } from "@/types";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type AvailabilityFilter = "all" | "available" | "unavailable";
type SortMode = "price" | "input" | "output" | "context" | "name";

const availabilityReasons: Record<string, string> = {
  no_active_accounts: "暂无可用账号",
  catalog_incomplete: "目录获取不完整",
  not_common_to_pool: "非账号池共同模型",
};

/** formatPrice renders one USD-per-million-token value. */
function formatPrice(value?: number): string {
  return value === undefined ? "—" : `$${value.toFixed(value < 0.2 ? 3 : 2)}`;
}

/** formatTokenCount renders model limits using the current Chinese locale. */
function formatTokenCount(value?: number): string {
  return value ? value.toLocaleString("zh-CN") : "—";
}

/** combinedPrice keeps models without a pricing snapshot at the end. */
function combinedPrice(model: CatalogModel): number {
  return model.pricing?.current.combined ?? Number.POSITIVE_INFINITY;
}

/** compareModels applies the selected stable ordering. */
function compareModels(
  left: CatalogModel,
  right: CatalogModel,
  mode: SortMode,
): number {
  if (mode === "name") return left.id.localeCompare(right.id);
  if (mode === "context") {
    return (
      (right.context_length ?? 0) - (left.context_length ?? 0) ||
      left.id.localeCompare(right.id)
    );
  }
  if (!left.pricing || !right.pricing) {
    if (!left.pricing && !right.pricing) return left.id.localeCompare(right.id);
    return left.pricing ? -1 : 1;
  }
  const leftPrice =
    mode === "price" ? combinedPrice(left) : left.pricing.current[mode];
  const rightPrice =
    mode === "price" ? combinedPrice(right) : right.pricing.current[mode];
  return leftPrice - rightPrice || left.id.localeCompare(right.id);
}

/** ModelsPage displays catalog pricing together with live pool availability. */
export function ModelsPage() {
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [availability, setAvailability] =
    useState<AvailabilityFilter>("all");
  const [sortMode, setSortMode] = useState<SortMode>("price");

  useEffect(() => {
    let active = true;
    api
      .getModels()
      .then((response) => active && setCatalog(response))
      .catch((requestError: unknown) => {
        if (active) {
          setError(
            requestError instanceof ApiError
              ? requestError.message
              : "模型目录加载失败",
          );
        }
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, []);

  /** handleRefresh synchronizes upstream models without resetting table controls. */
  async function handleRefresh() {
    setRefreshing(true);
    setError("");
    try {
      const response = await api.refreshModels();
      setCatalog(response);
      toast.success(`已同步 ${response.available} 个可用模型`);
    } catch (requestError: unknown) {
      const message =
        requestError instanceof ApiError
          ? requestError.message
          : "模型目录刷新失败";
      setError(message);
      toast.error(message);
    } finally {
      setRefreshing(false);
    }
  }

  const models = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return [...(catalog?.models ?? [])]
      .filter((model) => {
        if (availability === "available" && !model.available) return false;
        if (availability === "unavailable" && model.available) return false;
        if (!normalizedQuery) return true;
        return [model.id, model.name, model.provider, model.canonical_id].some(
          (value) => value?.toLowerCase().includes(normalizedQuery),
        );
      })
      .sort((left, right) => compareModels(left, right, sortMode));
  }, [availability, catalog, query, sortMode]);

  if (loading) {
    return (
      <div className="p-6 space-y-4">
        <Skeleton className="h-9 w-48" />
        <Skeleton className="h-[420px] w-full" />
      </div>
    );
  }

  return (
    <div className="p-4 md:p-6 space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <Boxes className="text-primary" size={24} />
            <h1 className="text-2xl font-semibold tracking-tight">模型列表</h1>
          </div>
          <p className="text-sm text-muted-foreground">
            价格单位为 USD / 1M tokens；强制刷新同步上游模型，价格仍使用当前快照。
          </p>
        </div>
        <Button
          variant="outline"
          disabled={refreshing}
          onClick={handleRefresh}
        >
          <RefreshCw className={refreshing ? "animate-spin" : ""} />
          {refreshing ? "正在同步…" : "强制刷新"}
        </Button>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {catalog && !catalog.availability_complete && (
        <Alert>
          <AlertDescription>
            当前账号池模型目录不完整；不可用状态仅作提示，请以实际上游响应为准。
          </AlertDescription>
        </Alert>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card>
          <CardContent className="p-4">
            <div className="text-2xl font-semibold">{catalog?.total ?? 0}</div>
            <div className="text-xs text-muted-foreground">目录模型</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="text-2xl font-semibold text-emerald-500">
              {catalog?.available ?? 0}
            </div>
            <div className="text-xs text-muted-foreground">当前可用</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="text-2xl font-semibold">
              {(catalog?.total ?? 0) - (catalog?.available ?? 0)}
            </div>
            <div className="text-xs text-muted-foreground">当前不可用</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="text-sm font-medium">
              {catalog?.pricing_updated_at
                ? new Date(catalog.pricing_updated_at).toLocaleDateString(
                    "zh-CN",
                  )
                : "—"}
            </div>
            <div className="text-xs text-muted-foreground">价格快照日期</div>
          </CardContent>
        </Card>
      </div>

      <div className="flex flex-col md:flex-row gap-3">
        <div className="relative flex-1">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
            size={16}
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索模型或 Provider"
            className="pl-9"
          />
        </div>
        <Select
          value={availability}
          onValueChange={(value) =>
            setAvailability(value as AvailabilityFilter)
          }
        >
          <SelectTrigger className="md:w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem value="available">当前可用</SelectItem>
            <SelectItem value="unavailable">当前不可用</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={sortMode}
          onValueChange={(value) => setSortMode(value as SortMode)}
        >
          <SelectTrigger className="md:w-52">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="price">当前合计价：低到高</SelectItem>
            <SelectItem value="input">当前输入价：低到高</SelectItem>
            <SelectItem value="output">当前输出价：低到高</SelectItem>
            <SelectItem value="context">上下文：大到小</SelectItem>
            <SelectItem value="name">模型名称</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <Card>
        <CardContent className="p-0 overflow-x-auto">
          <Table className="min-w-[1620px]">
            <TableHeader>
              <TableRow>
                <TableHead>模型</TableHead>
                <TableHead>计费渠道</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>免费账号</TableHead>
                <TableHead className="text-right">上下文</TableHead>
                <TableHead className="text-right">最大输出</TableHead>
                <TableHead className="text-right">输入原价</TableHead>
                <TableHead className="text-right">输出原价</TableHead>
                <TableHead className="text-right">当前输入</TableHead>
                <TableHead className="text-right">当前输出</TableHead>
                <TableHead className="text-right">当前合计</TableHead>
                <TableHead className="text-right">折扣</TableHead>
                <TableHead>促销截止</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {models.map((model) => (
                <TableRow key={model.id}>
                  <TableCell>
                    <div className="font-mono text-sm font-medium">
                      {model.id}
                    </div>
                    {model.name && (
                      <div className="text-xs text-muted-foreground mt-1">
                        {model.name}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>{model.provider || model.owned_by}</TableCell>
                  <TableCell>
                    {model.available ? (
                      <Badge className="bg-emerald-500/15 text-emerald-600 hover:bg-emerald-500/15">
                        可用
                      </Badge>
                    ) : (
                      <div className="space-y-1">
                        <Badge variant="secondary">不可用</Badge>
                        <div className="text-xs text-muted-foreground">
                          {availabilityReasons[
                            model.availability_reason ?? ""
                          ] ?? "不可用"}
                        </div>
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    {model.free_account_callable ? (
                      <Badge className="bg-emerald-500/15 text-emerald-600 hover:bg-emerald-500/15">
                        免费账号可调用
                      </Badge>
                    ) : (
                      <Badge variant="secondary">免费账号不可调用</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatTokenCount(model.context_length)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatTokenCount(model.max_completion_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatPrice(model.pricing?.base.input)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatPrice(model.pricing?.base.output)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatPrice(model.pricing?.current.input)}
                  </TableCell>
                  <TableCell className="text-right font-mono">
                    {formatPrice(model.pricing?.current.output)}
                  </TableCell>
                  <TableCell className="text-right font-mono font-semibold">
                    {formatPrice(model.pricing?.current.combined)}
                  </TableCell>
                  <TableCell className="text-right">
                    {model.pricing?.discount_percent ? (
                      <Badge variant="outline" className="text-orange-500">
                        -{model.pricing.discount_percent}%
                      </Badge>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell>
                    {model.pricing?.promotion_ends_at
                      ? new Date(
                          model.pricing.promotion_ends_at,
                        ).toLocaleDateString("zh-CN")
                      : "—"}
                  </TableCell>
                </TableRow>
              ))}
              {models.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={12}
                    className="h-24 text-center text-muted-foreground"
                  >
                    没有符合条件的模型
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
