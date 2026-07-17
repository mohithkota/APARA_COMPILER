long long results[8];
int fact(int n){ if(n<=1) return 1; return n*fact(n-1); }
int fib(int n){ if(n<2) return n; return fib(n-1)+fib(n-2); }
int isodd(int n);
int iseven(int n){ if(n==0) return 1; return isodd(n-1); }
int isodd(int n){ if(n==0) return 0; return iseven(n-1); }
int sumto(int n){ if(n==0) return 0; return n+sumto(n-1); }
int main(){
    results[0]=fact(5);   /* 120 */
    results[1]=fact(10);  /* 3628800 */
    results[2]=fib(10);   /* 55 */
    results[3]=fib(15);   /* 610 */
    results[4]=iseven(10);/* 1 */
    results[5]=isodd(7);  /* 1 */
    results[6]=sumto(50); /* 1275 */
    results[7]=sumto(30); /* 465 */
    return 1;
}
