typedef int myint; typedef struct{int a;int b;} Pair; long long results[3];
int main(){ myint x=9; Pair p; p.a=4;p.b=5; results[0]=x; results[1]=p.a; results[2]=p.b; return 1; } /*9,4,5*/
