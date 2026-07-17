long long results[3];
int classify(int n){ int r=0; switch(n){case 1:r+=1;case 2:r+=2;break;case 3:r+=3;default:r+=10;} return r; }
int main(){ results[0]=classify(1);results[1]=classify(2);results[2]=classify(3); return 1; } /*3,2,13*/
