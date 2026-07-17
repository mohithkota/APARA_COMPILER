long long results[4];
int main(){
    int a=1,b=2,c=3,d=4,e=5,f=6,g=7,h=8,i=9,j=10,k=11,l=12,m=13,n=14,o=15,p=16;
    int q=17,r=18,s=19,t=20,u=21,v=22,w=23,x=24,y=25,z=26,aa=27,bb=28,cc=29,dd=30;
    /* a big expression keeping many live at once */
    long long r1 = (long long)a+b*c-d+e*f-g+h*i-j+k*l-m+n*o-p;
    long long r2 = (long long)q+r*s-t+u*v-w+x*y-z+aa*bb-cc+dd;
    long long r3 = r1*2 + r2*3 - (a+b+c+d+e+f+g+h+i+j);
    results[0]=r1; results[1]=r2; results[2]=r3;
    results[3]=a+b+c+d+e+f+g+h+i+j+k+l+m+n+o+p+q+r+s+t+u+v+w+x+y+z; /* 351 */
    return 1;
}
