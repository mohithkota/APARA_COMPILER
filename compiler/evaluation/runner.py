"""runner.py -- the R4.6.5 benchmark suite and driver."""
import os, sys, csv
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE); sys.path.insert(0, os.path.dirname(_HERE))
import metrics

W = 32

def _ew(expr, T='vi8_t', N=64, decls='a,b,c,d,e'):
    ds = ','.join(f"{v}[{N+8}]" for v in decls.split(','))
    return f"long long f(){{{T} {ds};int i;for(i=0;i<{N-4};i++){expr};return e[0];}}"

def _tap(n, N=64, T='vi8_t'):
    e = '+'.join(f"in[i+{k}]" for k in range(n))
    return f"long long f(){{{T} in[{N+8}],out[{N+8}];int i;for(i=0;i<{N-n};i++)out[i]={e};return out[0];}}"

def _gemm(T, M, K, N):
    return (f"long long f(){{{T} A[{M*K}],B[{K*N}],C[{M*N}];int i,j,k,s;"
            f"for(i=0;i<{M};i++)for(k=0;k<{K};k++){{s=A[i*{K}+k];"
            f"for(j=0;j<{N};j++)C[i*{N}+j]+=s*B[k*{N}+j];}}return C[0];}}")

SUITE = [
 # (name, family, source)
 ("dot vi8",        "dot",        "long long f(){vi8_t a[64],b[64];int i;long long s=0;for(i=0;i<64;i++)s+=a[i]*b[i];return s;}"),
 ("dot vi16",       "dot",        "long long f(){vi16_t a[64],b[64];int i;long long s=0;for(i=0;i<64;i++)s+=a[i]*b[i];return s;}"),
 ("reduction vi8",  "reduction",  "long long f(){vi8_t a[64];int i;long long s=0;for(i=0;i<64;i++)s+=a[i];return s;}"),
 ("reduction vi32", "reduction",  "long long f(){vi32_t a[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i];return s;}"),
 ("elementwise add","elementwise",_ew("e[i]=a[i]+b[i]")),
 ("elementwise mul","elementwise",_ew("e[i]=a[i]*b[i]")),
 ("elementwise copy","elementwise",_ew("e[i]=a[i]")),
 ("expr a+b+c",     "expression", _ew("e[i]=a[i]+b[i]+c[i]")),
 ("expr a*b+c",     "expression", _ew("e[i]=a[i]*b[i]+c[i]")),
 ("expr (a+b)*c",   "expression", _ew("e[i]=(a[i]+b[i])*c[i]")),
 ("expr a+b+c+d",   "expression", _ew("e[i]=a[i]+b[i]+c[i]+d[i]")),
 ("axpy vi8",       "axpy",       "long long f(){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}"),
 ("axpy vi16",      "axpy",       "long long f(){vi16_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}"),
 ("axpy remainder", "axpy",       "long long f(){vi8_t X[24],Y[24];int i;int a=3;for(i=0;i<20;i++)Y[i]+=a*X[i];return Y[0];}"),
 ("gemm vi8 16^3",  "gemm",       _gemm('vi8_t',16,16,16)),
 ("gemm vi8 8x8x32","gemm",       _gemm('vi8_t',8,8,32)),
 ("gemm vi16",      "gemm",       _gemm('vi16_t',8,8,16)),
 ("conv 3-tap",     "convolution",_tap(3)),
 ("conv 5-tap",     "convolution",_tap(5)),
 ("conv 7-tap",     "convolution",_tap(7)),
 ("conv 3-tap vi16","convolution",_tap(3,32,'vi16_t')),
 # intentionally NOT vectorizable -- the control/scalar baseline
 ("scalar bubblesort","scalar",   "long long f(){int a[32];int i,j,t;for(i=0;i<32;i++)a[i]=32-i;for(i=0;i<31;i++)for(j=0;j<31-i;j++)if(a[j]>a[j+1]){t=a[j];a[j]=a[j+1];a[j+1]=t;}return a[0];}"),
 ("scalar gcd",     "scalar",     "long long f(){int a=1071,b=462,t;while(b){t=b;b=a%b;a=t;}return a;}"),
 ("scalar binsearch","scalar",    "long long f(){int a[64];int i,lo=0,hi=63,m,k=37;for(i=0;i<64;i++)a[i]=i*2;while(lo<=hi){m=(lo+hi)/2;if(a[m]==k)return m;if(a[m]<k)lo=m+1;else hi=m-1;}return -1;}"),
 ("scalar popcount","scalar",     "long long f(){unsigned x=0xdeadbeef;int c=0;while(x){c+=x&1;x>>=1;}return c;}"),
 ("scalar divmod",  "scalar",     "long long f(){int i;long long s=0;for(i=1;i<64;i++)s+=(1000/i)+(1000%i);return s;}"),
]


def run(out_dir=None):
    out_dir = out_dir or os.path.join(_HERE, 'results')
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for name, fam, src in SUITE:
        vec = metrics.measure(name, src, vectorize=True)
        sca = metrics.measure(name, src, vectorize=False)
        orc = metrics.oracle_of(src) or {}
        r = dict(family=fam, **vec)
        r['scalar_ipb'] = sca['ipb']
        r['scalar_bundles'] = sca['static_bundles']
        r['scalar_code_size'] = sca['code_size']
        r['vector_ipb'] = vec['ipb']
        for k in ('theoretical_ipb', 'local_ideal_ipb', 'achieved_ipb',
                  'utilization', 'pipelining_gap', 'scheduler_gap',
                  'limiter', 'top_opportunity', 'n_loops'):
            r[k] = orc.get(k, '')
        rows.append(r)
    path = os.path.join(out_dir, 'benchmarks.csv')
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return rows, path


if __name__ == '__main__':
    rows, path = run()
    print(f"wrote {len(rows)} rows -> {path}")
