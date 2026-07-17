struct Inner{int x;int y;}; struct Outer{struct Inner in;int z;}; long long results[3];
int main(){ struct Outer o; o.in.x=1;o.in.y=2;o.z=3; results[0]=o.in.x;results[1]=o.in.y;results[2]=o.z; return 1; } /*1,2,3*/
