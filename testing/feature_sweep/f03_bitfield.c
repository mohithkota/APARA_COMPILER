struct B{unsigned a:3;unsigned b:5;}; long long results[2];
int main(){ struct B x; x.a=5; x.b=20; results[0]=x.a; results[1]=x.b; return 1; } /*5,20*/
