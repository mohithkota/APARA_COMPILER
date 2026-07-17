union U{int a;int b;}; long long results[2];
int main(){ union U u; u.a=42; results[0]=u.a; results[1]=u.b; return 1; } /*42,42*/
